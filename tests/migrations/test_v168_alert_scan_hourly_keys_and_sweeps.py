"""Locks for V168 -- SP_ALERT_SCAN keys and sweeps (round-2 review, alerts cluster: R2-034, R2-035, R2-036,
R2-039, R2-040, R2-091), one re-derivation from V162 (its current definer).

STRUCTURE + GENERATION + LOCKS; tests/migrations/test_v168_harness.py EXECUTES the arms and sweeps. What this
file proves:
  * generation -- outputs/gen_v168.py regenerates the migration byte-for-byte and reads only its base; the
    read-only PREFLIGHT / PART B and the commented-out owner repair are written only on request and parse;
  * shape -- first line, guard (-20168, v < 167), the marker + SP_ALERT_SCAN, the two guarded NAME refreshes, the
    version row; nothing runs at apply time and no ALERT_EVENTS row is written;
  * lineage + round 13 -- reversing every declared delta (the test's OWN copies) gives V162's body back
    byte-for-byte, and a stray edit anywhere else breaks the compare;
  * the deltas -- each arm / sweep lock, the untouched arms, the tally, the footprint, the keys' widths.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests._source import ROOT, read
from tests.test_migration_proc_syntax import _strip_noise

_NAME = "V168__alert_scan_hourly_keys_and_sweeps.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V162 = read("snowflake/migrations/V162__security_takeover_admin_grant.sql")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _body(proc: str) -> str:
    return proc[proc.index("$$") + 2:proc.rindex("$$")]


_H = _proc(_MIG, "SP_ALERT_SCAN()")
_H162 = _proc(_V162, "SP_ALERT_SCAN()")

# ---------------------------------------------------------------------------------------------------
# Test-side copies of every declared delta (independent of outputs/gen_v168.py).
# ---------------------------------------------------------------------------------------------------
_MARKER = ("-- >>> derived:SP_ALERT_SCAN  (from V162; [14] failure-day key, [18] outcome + first-seen-day key + 48h "
           "episode guard, [20] pointer, V067 FAILED supersede, V091 sweep any raise day, dead prologue reads, V168)\n")
_A14 = ("    -- [14] PIPE_COPY_FAILURES\n", "    -- [17] COST_DEPT_BUDGET_PACE\n")
_A18 = ("    -- [18] SEC_NEW_ADMIN_NETWORK", "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]")
_A20 = ("    -- [20] SEC_NEW_EXPOSURE", "    END IF;   -- /V157 cadence gate: [20]")

_DELTAS: list[tuple[str, str, tuple[str, str] | None]] = [
    # (V162 text, V168 text, arm slice or None for a whole-body unique anchor)
    ("    budget_usd FLOAT;\n    credit_price FLOAT;\n    ai_credit_price FLOAT;\n", "    credit_price FLOAT;\n",
     None),
    ("    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'MONTHLY_BUDGET_USD', VALUE, NULL))), 0),\n"
     "           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68),\n"
     "           COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'AI_CREDIT_PRICE_USD', VALUE, NULL))), 2.20)\n"
     "      INTO :budget_usd, :credit_price, :ai_credit_price\n",
     "    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)   -- V168: the rate "
     "arm [17] binds\n      INTO :credit_price\n", None),
    ("        -- PIPE_COPY_FAILURES: failed or partial file loads in the last 24h.\n"
     "        -- Broken ingestion is the most preventable 'found out too late' class.\n",
     "        -- PIPE_COPY_FAILURES: failed or partial file loads per Central failure day (yesterday + today).\n"
     "        -- Broken ingestion is the most preventable 'found out too late' class. V168: keyed by the day the\n"
     "        -- files FAILED over whole Central days (not the scan day over a rolling 24h), so the count of a day "
     "only\n        -- grows -- the same files never re-raise after midnight and a band never steps back down.\n", _A14),
    ("' failed file load(s) (24h)',\n", "' failed file load(s) on ' || TO_VARCHAR(p.FAIL_DAY),\n", _A14),
    ("'CRIT', 'WARN') || '|' || TO_VARCHAR(CURRENT_DATE())  -- V066 #1: band matches the CRITICAL severity so a "
     "HIGH->CRITICAL crossing re-fires\n",
     "'CRIT', 'WARN') || '|' || TO_VARCHAR(p.FAIL_DAY)  -- V066 #1: band matches the CRITICAL severity so a "
     "HIGH->CRITICAL crossing re-fires; V168: the failure day\n", _A14),
    ("TABLE_NAME AS TBL,\n                   MAX(PIPE_NAME) AS PIPE,\n",
     "TABLE_NAME AS TBL,\n                   TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME)) AS FAIL_DAY,\n"
     "                   MAX(PIPE_NAME) AS PIPE,\n", _A14),
    ("            WHERE LAST_LOAD_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())\n",
     "            WHERE LAST_LOAD_TIME >= DATEADD('hour', -50, CURRENT_TIMESTAMP())   -- V168: prune only; yesterday "
     "00:00 Central is at most 49h back (DST fall-back included)\n"
     "              AND TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME))\n"
     "                  >= DATEADD('day', -1, TO_DATE(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())))\n",
     _A14),
    ("            GROUP BY 1, 2, 3\n", "            GROUP BY 1, 2, 3, 4\n", _A14),
    ("the r25 panel, with teeth)\n    BEGIN\n",
     "the r25 panel, with teeth)\n"
     "    --      V168 (R2-039): SUCCESSES counts the attempts that got in; the TITLE says 'logged in' only then, else\n"
     "    --      'N failed login attempt(s) from new network <IP> (0 successful)'. A failures-only pair keys\n"
     "    --      user|IP|FAILED|<day>, so a later success in the same 24h raises its own event (the V067 sweep then\n"
     "    --      supersedes the failed one). V168 (R2-036): the key ends in the pair's first-seen Central day, so a\n"
     "    --      network quiet 90+ days alerts again (the rule name, playbook and Security panel promise the\n"
     "    --      re-flag; the undated V043 key matched the pair's first event forever). The 48h guard compares the\n"
     "    --      EXACT date-stripped base, so a late earlier login across Central midnight, or a pre-V168 undated\n"
     "    --      key, never raises one episode twice, and a failures-only key never swallows the success.\n"
     "    BEGIN\n", _A18),
    ("               nn.USER_NAME || ' logged in from new network ' || nn.CLIENT_IP,\n"
     "               'First seen ' || nn.FIRST_SEEN || ' against a 90d baseline. Auth: '\n"
     "                   || COALESCE(nn.AUTH_FACTOR, '?')\n"
     "                   || '. Expected after travel/VPN/host changes; anything else is the finding.',\n"
     "               nn.LOGINS,\n"
     "               c.RULE_ID || '|' || nn.USER_NAME || '|' || nn.CLIENT_IP\n",
     "               LEFT(nn.USER_NAME || IFF(nn.SUCCESSES > 0,\n"
     "                   ' logged in from new network ' || nn.CLIENT_IP,\n"
     "                   ': ' || nn.LOGINS || ' failed login attempt(s) from new network ' || nn.CLIENT_IP\n"
     "                       || ' (0 successful)'), 300),\n"
     "               'First seen ' || nn.FIRST_SEEN || ' against a 90d baseline; successful '\n"
     "                   || nn.SUCCESSES || ' of ' || nn.LOGINS || ' attempt(s). Auth: '\n"
     "                   || COALESCE(nn.AUTH_FACTOR, '?')\n"
     "                   || IFF(nn.SUCCESSES > 0,\n"
     "                          '. Expected after travel/VPN/host changes; anything else is the finding.',\n"
     "                          '. No attempt got in; a success from this IP inside its first 24h raises a "
     "separate event.'),\n"
     "               nn.LOGINS,\n"
     "               c.RULE_ID || '|' || LEFT(nn.USER_NAME, 200) || '|' || nn.CLIENT_IP || IFF(nn.SUCCESSES > 0, "
     "'', '|FAILED')\n"
     "                   || '|' || TO_VARCHAR(TO_DATE(CONVERT_TIMEZONE('America/Chicago', nn.FIRST_SEEN)), "
     "'YYYY-MM-DD')\n", _A18),
    ("                   COUNT(*) AS LOGINS,\n",
     "                   COUNT(*) AS LOGINS,\n                   COUNT_IF(L.IS_SUCCESS = 'YES') AS SUCCESSES,\n", _A18),
    ("            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n        );\n",
     "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
     "               OR (e.RULE_ID = b.RULE_ID\n"
     "                   AND e.RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())\n"
     "                   AND (e.DEDUPE_KEY = REPLACE(LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 11), '|FAILED', '')\n"
     "                        OR (LENGTH(e.DEDUPE_KEY) = LENGTH(b.DEDUPE_KEY)\n"
     "                            AND LEFT(e.DEDUPE_KEY, LENGTH(e.DEDUPE_KEY) - 10)\n"
     "                                = LEFT(b.DEDUPE_KEY, LENGTH(b.DEDUPE_KEY) - 10))))\n"
     "        );\n", _A18),
    ("GRANTS_TO_ROLES - review in Security -> Access.'",
     "GRANTS_TO_ROLES - review in Security -> Changes (Recent grant changes).'", _A20),
    ("REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED'))\n",
     "REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')\n"
     "                      -- V168 (R2-039): a failures-only SEC_NEW_ADMIN_NETWORK event (user|IP|FAILED|<day>) is\n"
     "                      -- superseded once the same user + IP success event (user|IP|<day>) opens within 48h of "
     "it.\n"
     "                      OR (lo.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'\n"
     "                          AND lo.DEDUPE_KEY LIKE '%|FAILED|____-__-__'\n"
     "                          AND LEFT(hi.DEDUPE_KEY, LENGTH(hi.DEDUPE_KEY) - 11)\n"
     "                              = REPLACE(LEFT(lo.DEDUPE_KEY, LENGTH(lo.DEDUPE_KEY) - 11), '|FAILED', '')\n"
     "                          AND hi.RAISED_AT >= lo.RAISED_AT\n"
     "                          AND hi.RAISED_AT <= DATEADD('hour', 48, lo.RAISED_AT)))\n", None),
    ("    -- [auto-clear sweep] V091: resolve TODAY's still-OPEN live-window events whose\n",
     "    -- [auto-clear sweep] V091: resolve still-OPEN live-window events (any raise day, V168) whose\n", None),
    ("    -- Only today's bucket (LIKE '%|<today>') is touched, so historical day-stamped\n"
     "    -- exceedances are never rewritten. RESOLUTION_KIND='AUTO_CLEARED' is excluded from\n",
     "    -- V168: no age bound (the V096 >= -48h bound is gone). Every OPEN event of the 3 rules is re-checked each\n"
     "    -- scan on its date-stripped identity (V119), so a multi-day or hysteresis-held condition never\n"
     "    -- strands its older day-stamped events OPEN. RESOLUTION_KIND='AUTO_CLEARED' is excluded from\n", None),
    ("           AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())     -- dwell: anti-flap\n"
     "           AND (ev.RULE_ID || '|' || SPLIT_PART(ev.DEDUPE_KEY, '|', 2)) NOT IN (\n",
     None, None),   # placeholder replaced below (the removed V096 line sits right above this pair)
    ("    -- only keys (IP, grant time) never end in a bare date so they are never stripped -- untouched.\n",
     "    -- only keys (grant time) never end in a bare date so they are never stripped -- untouched. The\n"
     "    -- [18] first-seen day (V168) strips to user|IP or user|IP|FAILED; a re-raise needs 90 quiet days.\n", None),
    ("'alert scan v13 (V162: + SEC_LOGIN_TAKEOVER + SEC_ADMIN_GRANT hourly; V157 gates unchanged): '",
     "'alert scan v14 (V168: [14] failure-day key, [18] outcome + first-seen-day key, auto-clear any raise day; "
     "V162 arms and V157 gates unchanged): '", None),
]
_V096_BOUND = ("           AND ev.RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())                    -- V096: "
               "recent window (was date-in-key); catches next-day-cleared 24h conditions\n")
_DWELL_PAIR = _DELTAS[16][0]
_DELTAS[16] = (_V096_BOUND + _DWELL_PAIR, _DWELL_PAIR, None)


def _reverse(h: str, deltas=None) -> str:
    """Reverse every declared V168 delta (asserting each new text is where it should be, once)."""
    for old, new, span in (deltas or _DELTAS):
        if span is None:
            assert h.count(new) == 1, new[:90]
            h = h.replace(new, old)
        else:
            i = h.index(span[0])
            j = h.index(span[1], i)
            seg = h[i:j]
            assert seg.count(new) == 1, new[:90]
            h = h[:i] + seg.replace(new, old) + h[j:]
    return h


def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("V168_OUT", "PREFLIGHT_OUT", "PART_B_OUT", "REPAIR_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v168.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


def _extras(tmp_path: Path) -> tuple[str, str, str]:
    pre, pb, rp = tmp_path / "PF.sql", tmp_path / "PB.sql", tmp_path / "RP.sql"
    result = _run_gen(tmp_path, V168_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pre), PART_B_OUT=str(pb),
                      REPAIR_OUT=str(rp))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG      # the extras never change the migration
    return pre.read_text(encoding="utf-8"), pb.read_text(encoding="utf-8"), rp.read_text(encoding="utf-8")


# -- generation ----------------------------------------------------------------------------------------------

def test_v168_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V168_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (ROOT / "snowflake" / "migrations" / _NAME).read_bytes(), (
        "V168 drifted from its forward-generation -- edit outputs/gen_v168.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]
    assert "PREFLIGHT" not in result.stdout and "PART B" not in result.stdout and "REPAIR" not in result.stdout


def test_v168_generator_reads_only_its_base():
    gen = read("outputs/gen_v168.py")
    assert re.findall(r'MIG / "(V\d+__\w+\.sql)"', gen) == ["V162__security_takeover_admin_grant.sql"]
    assert "import app" not in gen and "from app" not in gen
    assert gen.count(".read_text(") == 1


@pytest.mark.parametrize("which", ["preflight", "part_b"])
def test_v168_preflight_and_part_b_are_read_only_and_parse(tmp_path, which):
    pre, part_b, _ = _extras(tmp_path)
    sql = pre if which == "preflight" else part_b
    code = _strip_noise(sql)
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT",
                   "REVOKE", "EXECUTE"):
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert not re.search(r"(?<![:\w]):[A-Za-z_]\w*", code), "a scripting :bind survived"
    assert "$$" not in sql and "@" not in sql and "\r" not in sql
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    parsed = [p for p in sqlglot.parse(sql, dialect="snowflake") if p is not None]
    assert len(parsed) == (5 if which == "preflight" else 3)
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    for tree in parsed:
        assert tree.key == "select" or isinstance(tree, exp.Union), tree.key
        assert not [type(n).__name__ for n in tree.walk() if isinstance(n, writes)]


def test_v168_preflight_carries_the_arm_and_sweep_text_verbatim(tmp_path):
    pre, _, _ = _extras(tmp_path)
    a14, a18 = _between(_H, *_A14), _between(_H, *_A18)
    src14 = _between(a14, "            SELECT TABLE_CATALOG_NAME AS DB", "        ) p ON c.RULE_ID")
    src18 = _between(a18, "            SELECT L.USER_NAME,", "        ) nn\n")
    assert src14 in pre and src18 in pre
    sweep = _between(_H, "    -- [auto-clear sweep] V091:", "    EXCEPTION\n")
    firing = sweep[sweep.index("               SELECT c.RULE_ID || '|' || q.COMPANY AS DEDUPE_KEY"):
                   sweep.rindex("           );\n")]
    assert firing in pre
    for grid in ("-- P168.1 ", "-- P168.2 ", "-- P168.3 ", "-- P168.4 "):
        assert pre.count(grid) == 1, grid


def test_v168_owner_repair_is_optional_and_fully_commented(tmp_path):
    _, _, rp = _extras(tmp_path)
    assert all(ln.startswith("--") for ln in rp.splitlines() if ln.strip())
    assert not _strip_noise(rp).strip()
    flat = " ".join(ln.lstrip("- ") for ln in rp.splitlines())
    assert "R168.1 OPTIONAL (owner decision)" in flat and "SNOOZED rows are left to wake" in flat
    assert "RESOLUTION_KIND = 'SUPERSEDED'" in flat and "never DELETE" in flat
    sqlglot = pytest.importorskip("sqlglot")
    lines = rp.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("-- UPDATE "))
    end = next(i for i in range(start, len(lines)) if lines[i].rstrip().endswith(";"))
    stmt = "\n".join(ln[3:] for ln in lines[start:end + 1])
    (tree,) = sqlglot.parse(stmt, dialect="snowflake")
    assert tree.key == "update"


# -- guard, order, shape -------------------------------------------------------------------------------------

def test_v168_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    guard162 = _between(_V162, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard162.replace("-20162", "-20168").replace("'V162 requires V161 first", "'V168 requires V167 first")
            .replace("IF (v < 161)", "IF (v < 167)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert "not_ready EXCEPTION (-20168, 'V168 requires V167 first - apply migrations in order.');" in _MIG
    assert _MIG.count("EXECUTE IMMEDIATE") == 1 and "SELECT 168 AS VERSION" in _MIG
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 168);")
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for word in ("WHY:", "COST:", "LATENCY:", "FIRST RUN:", "ROLLBACK:", "Apply AFTER V167. Idempotent; safe to re-run."):
        assert word in header, word


def test_v168_file_order_and_statements():
    from tests.test_migrations_parse import _plain_statements
    guard = _MIG.index("EXCEPTION (-20168")
    mark = _MIG.index(_MARKER)
    scan = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()")
    names = _MIG.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG")
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < mark < scan < names < version
    assert _MIG[mark + len(_MARKER):].startswith("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()")
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 3)[:3] for s in _plain_statements(_MIG)]
    assert kinds == [["UPDATE", "DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG", "SET"],
                     ["UPDATE", "DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG", "SET"],
                     ["INSERT", "INTO", "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION"]], kinds
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 1 and _MIG.count("$$") == 4
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "ALTER", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "DELETE", "MERGE", "VIEW",
                   "FUNCTION", "TABLE"):
        assert not re.search(rf"\b{banned}\b", top), banned
    assert "ALERT_EVENTS" not in top                                    # no data write, no repair at apply
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG) and "\r" not in _MIG
    sqlglot = pytest.importorskip("sqlglot")
    for plain in _plain_statements(_MIG):
        (tree,) = sqlglot.parse(plain, dialect="snowflake")
        assert tree is not None


def test_v168_name_refreshes_are_guarded_on_the_seed_text():
    v011 = read("snowflake/migrations/V011__proactive_alerts.sql")
    v043 = read("snowflake/migrations/V043__task_retirement_alert_teeth.sql")
    seeds = {"PIPE_COPY_FAILURES": "Failed COPY / Snowpipe file loads in 24h (threshold = allowed failures)",
             "SEC_NEW_ADMIN_NETWORK": "Admin login from a network unseen in 90 days"}
    new = {"PIPE_COPY_FAILURES": "Failed COPY / Snowpipe file loads per Central failure day (threshold = allowed "
                                 "failures)",
           "SEC_NEW_ADMIN_NETWORK": "Admin login attempt from a network unseen in 90 days (the title says whether "
                                    "any succeeded)"}
    assert f"'{seeds['PIPE_COPY_FAILURES']}'" in v011 and f"'{seeds['SEC_NEW_ADMIN_NETWORK']}'" in v043
    for rule in seeds:
        stmt = (f"UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n   SET NAME = '{new[rule]}'\n"
                f" WHERE RULE_ID = '{rule}'\n   AND NAME = '{seeds[rule]}';\n")
        assert _MIG.count(stmt) == 1, rule
        assert len(new[rule]) <= 200 and "'" not in new[rule]                # V004 NAME VARCHAR(200)
    assert "DESCRIPTION =" not in _MIG.split("$$")[-1].split("INSERT INTO")[0]   # ALERT_CONFIG has no DESCRIPTION
    # the config replay (Guards A-D) reads both UPDATEs as text-only: no ENABLED flip, both rules stay live
    from tests.test_alert_rule_consistency import _config_enabled, _raised_rule_ids
    assert {"PIPE_COPY_FAILURES", "SEC_NEW_ADMIN_NETWORK"} <= _config_enabled() & _raised_rule_ids()


def test_v168_description_fits_and_doubles_apostrophes():
    desc = re.search(r"SELECT 168 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc and len(desc) <= 4000
    assert "re-derived from V162" in desc and "tally 14 unchanged" in desc


# -- lineage + round 13 ------------------------------------------------------------------------------------

def test_v168_marker_names_the_current_definer():
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _migrations, _violations
    texts = _migrations()
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    r = rows[(168, "SP_ALERT_SCAN")]
    assert r["src"] == "marker" and r["claims"] == [162] and r["prev"] == 162 and not r["waived"], r
    assert [v for v in _definers(texts)["SP_ALERT_SCAN"] if 162 <= v <= 168] == [162, 168]
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V168 ")]
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 1


def test_v168_hourly_normalizes_back_to_v162_byte_for_byte():
    assert _reverse(_H) == _H162


@pytest.mark.parametrize("victim", ["'rule PIPE_COPY_FAILURES - other rules unaffected'", "SP_SCAN_ETL_CYCLE",
                                    "AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')",
                                    "nn.LOGINS >= c.THRESHOLD_NUM", "HOUR_TS >= DATEADD('hour', -24"])
def test_v168_normalize_check_has_teeth(victim):
    """Deleting one character anywhere outside the declared deltas breaks the byte compare."""
    assert _H.count(victim) >= 1
    assert _reverse(_H.replace(victim, victim[:-1], 1)) != _H162


@pytest.mark.parametrize("i", range(19))
def test_v168_every_declared_delta_is_real(i):
    """Skipping any one delta's reversal leaves a body that is NOT V162's."""
    assert _reverse(_H, [d for k, d in enumerate(_DELTAS) if k != i]) != _H162


# -- the deltas ----------------------------------------------------------------------------------------------

def test_v168_prologue_reads_only_the_rate_arm_17_binds():
    assert ":budget_usd" not in _H and ":ai_credit_price" not in _H
    assert "budget_usd FLOAT;" not in _H and "ai_credit_price FLOAT;" not in _H
    assert "'MONTHLY_BUDGET_USD'" not in _H and "'AI_CREDIT_PRICE_USD'" not in _H
    assert _H.count("INTO :credit_price\n") == 1 and _H.count(":credit_price") == 2
    arm17 = _between(_H, "    -- [17] COST_DEPT_BUDGET_PACE", "    -- [18] SEC_NEW_ADMIN_NETWORK")
    assert "b.MONTHLY_BUDGET_USD AS BUDGET_USD" in arm17 and "* :credit_price AS MTD_USD" in arm17
    # the daily scan keeps both live variables (budget pace / forecast / AI price)
    daily = _proc(read("snowflake/migrations/V163__ai_runaway_trust_regression.sql"), "SP_ALERT_SCAN_DAILY()")
    assert "INTO :budget_usd, :credit_price, :ai_credit_price" in daily


def test_v168_arm14_is_keyed_by_the_central_failure_day():
    a = _between(_H, *_A14)
    assert "TO_VARCHAR(CURRENT_DATE())" not in a and "DATEADD('hour', -24" not in a
    key = _between(a, "               c.RULE_ID || '|' || p.DB", "\n")
    assert key.split("  -- ")[0].endswith("|| '|' || TO_VARCHAR(p.FAIL_DAY)")
    assert "IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN')" in key                    # the band the V067 sweep reads
    assert "IFF(p.FAILED_FILES >= 10, 'CRITICAL', c.SEVERITY)" in a and "p.FAILED_FILES > c.THRESHOLD_NUM" in a
    bucket = "TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME))"
    assert a.count(bucket) == 2                                                  # the bucket IS the window
    assert f"{bucket} AS FAIL_DAY" in a
    assert ("AND TO_DATE(CONVERT_TIMEZONE('America/Chicago', LAST_LOAD_TIME))\n"
            "                  >= DATEADD('day', -1, TO_DATE(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())))")\
        in a
    assert "WHERE LAST_LOAD_TIME >= DATEADD('hour', -50, CURRENT_TIMESTAMP())" in a
    assert "GROUP BY 1, 2, 3, 4\n" in a and "' failed file load(s) on ' || TO_VARCHAR(p.FAIL_DAY)," in a
    assert "STATUS IN ('Load failed', 'Partially loaded')" in a
    assert _H.count("GROUP BY 1, 2, 3\n") == 1                                  # the other arm keeps its own


def test_v168_arm18_outcome_key_and_episode_guard():
    a = _between(_H, *_A18)
    assert a.count("COUNT_IF(L.IS_SUCCESS = 'YES') AS SUCCESSES") == 1
    assert "nn.LOGINS >= c.THRESHOLD_NUM" in a and "HAVING MIN(L.EVENT_TIMESTAMP) >= DATEADD('hour', -24" in a
    assert "L.EVENT_TIMESTAMP >= DATEADD('day', -90, CURRENT_TIMESTAMP())" in a
    assert re.findall(r"\bROLE IN \(([^)]*)\)", a) == ["'ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS'"]
    # the success title is V162's text, the failed one names the count and the 0
    assert "' logged in from new network ' || nn.CLIENT_IP," in a
    assert "' failed login attempt(s) from new network ' || nn.CLIENT_IP" in a and "' (0 successful)'" in a
    assert "baseline; successful '\n                   || nn.SUCCESSES || ' of ' || nn.LOGINS || ' attempt(s). Auth: '" in a
    # the key: outcome token BEFORE the explicit-format Central first-seen day
    assert ("c.RULE_ID || '|' || LEFT(nn.USER_NAME, 200) || '|' || nn.CLIENT_IP || IFF(nn.SUCCESSES > 0, '', "
            "'|FAILED')\n                   || '|' || TO_VARCHAR(TO_DATE(CONVERT_TIMEZONE('America/Chicago', "
            "nn.FIRST_SEEN)), 'YYYY-MM-DD')\n") in a
    # the guard compares the EXACT date-stripped base -- never a prefix test (a failures-only key starts with the
    # success base and would swallow the success event)
    assert "STARTSWITH" not in a and " LIKE " not in a
    assert "AND e.RAISED_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP())" in a
    assert "LENGTH(e.DEDUPE_KEY) = LENGTH(b.DEDUPE_KEY)" in a
    # every other arm keeps the plain key-only NOT EXISTS
    plain = ("        WHERE NOT EXISTS (\n            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
             "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n        );\n")
    assert _H.count(plain) == _H162.count(plain) - 1 == 8
    assert "e.RAISED_AT >= DATEADD('hour', -48" not in _H.replace(a, "")


def test_v168_arm20_points_where_investigate_lands():
    a = _between(_H, *_A20)
    assert a.count("GRANTS_TO_ROLES - review in Security -> Changes (Recent grant changes).'") == 1
    assert "review in Security -> Access" not in a
    arm26 = _between(_H, "    -- [26] SEC_LOGIN_TAKEOVER", "    -- [27] SEC_ADMIN_GRANT")
    assert "LOGIN_HISTORY - review in Security -> Access" in arm26              # [26] stays on Access


def test_v168_v067_sweep_supersedes_a_failures_only_new_network_event_only():
    sweep = _between(_H, "    -- V067 #40: supersede", "    -- [auto-clear sweep] V091:")
    for pair in ("REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')", "REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|')",
                 "REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')", "hi.RULE_ID = lo.RULE_ID",
                 "hi.DEDUPE_KEY <> lo.DEDUPE_KEY", "lo.STATUS IN ('OPEN', 'ACK')"):
        assert pair in sweep, pair
    assert sweep.count("lo.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'") == 1
    assert "lo.DEDUPE_KEY LIKE '%|FAILED|____-__-__'" in sweep
    assert "hi.RAISED_AT <= DATEADD('hour', 48, lo.RAISED_AT)" in sweep


def test_v168_auto_clear_sweep_has_no_age_bound():
    sweep = _between(_H, "    -- [auto-clear sweep]", "'V091 auto-clear sweep - other rules unaffected'")
    assert "ev.RAISED_AT >=" not in sweep
    assert sweep.count("ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())") == 1
    for frag in ("ev.STATUS = 'OPEN'", "AND ev.RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')",
                 "(ev.RULE_ID || '|' || SPLIT_PART(ev.DEDUPE_KEY, '|', 2)) NOT IN (",
                 "WHERE ENABLED AND AUTO_CLEAR_ENABLED", "RESOLUTION_KIND = 'AUTO_CLEARED'"):
        assert frag in sweep, frag
    assert sweep.count("HOUR_TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())") == 3
    assert "Only today's bucket" not in sweep and "any raise day, V168" in sweep


def test_v168_raise_arms_03_to_05_keep_their_day_keys_and_reraise_guard():
    for start, end in (("    -- [03] PERF_QUERY_FAIL_PCT", "    -- [04] PERF_QUEUED_MINUTES"),
                       ("    -- [04] PERF_QUEUED_MINUTES", "    -- [05] PERF_SPILL_GB"),
                       ("    -- [05] PERF_SPILL_GB", "    -- [10] SEC_CRED_EXPIRY")):
        arm = _between(_H, start, end)
        assert arm == _between(_H162, start, end), start
        assert "'|' || CURRENT_DATE()" in arm or "TO_VARCHAR(CURRENT_DATE())" in arm, start
        assert "AUTO_CLEARED" in arm, start


def test_v168_leaves_every_other_block_alone():
    for start, end in (("    -- [01] COST_DAILY_CREDITS", "    -- [03] PERF_QUERY_FAIL_PCT"),
                       ("    -- [10] SEC_CRED_EXPIRY", "    -- [14] PIPE_COPY_FAILURES"),
                       ("    -- [17] COST_DEPT_BUDGET_PACE", "    -- [18] SEC_NEW_ADMIN_NETWORK"),
                       ("    -- [21] SEC_POSTURE_METRIC", "    -- V067 #40: supersede"),
                       ("    -- [condition-ended sweep] V157", "    -- [snooze carry-forward sweep] V117"),
                       ("    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev\n           SET STATUS = 'SNOOZED'",
                        "    RETURN ")):
        assert _between(_H, start, end) == _between(_H162, start, end), start
    assert _H.count("IF (MOD(ct_hour, 4) = 1) THEN") == 4 and _H.count("IF (MOD(ct_hour, 3) = 2) THEN") == 1


def test_v168_tally_and_footprint_unchanged():
    from tests.test_alert_rule_consistency import _FAILS_INC_RE, _SCAN_DENOMINATORS
    assert len(_FAILS_INC_RE.findall(_H)) == 14
    self_alert_re, return_re = _SCAN_DENOMINATORS["SP_ALERT_SCAN"]
    assert {int(x) for x in re.findall(self_alert_re, _H)} == {14}
    assert re.findall(return_re, _H) == [("14", "14")] * 3
    assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _body(_H))) == {
        "COPY_HISTORY", "CREDENTIALS", "GRANTS_TO_ROLES", "GRANTS_TO_USERS", "LOGIN_HISTORY"}
    assert "alert scan v14 (V168:" in _H and "alert scan v13" not in _H
    assert "$$" not in _body(_H) and "\\" not in _body(_H)


def test_v168_changed_arm_statements_parse():
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    for span in (_A14, _A18, _A20):
        arm = _between(_H, *span)
        stmt = arm[arm.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):arm.index(";\n    EXCEPTION")]
        (parsed,) = sqlglot.parse(stmt, dialect="snowflake")
        assert [c.name for c in parsed.this.expressions] == ["RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL",
                                                             "METRIC_VALUE", "DEDUPE_KEY"]
        (b,) = [s for s in parsed.find_all(exp.Subquery) if s.alias == "b"]
        assert len(b.this.expressions) == 7
    for start in ("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS lo\n", "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev\n"
                  "           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'AUTO_CLEARED'"):
        stmt = _H[_H.index(start):_H.index(";\n", _H.index(start))]
        (parsed,) = sqlglot.parse(stmt, dialect="snowflake")
        assert parsed.key == "update"


# -- keys: widths and the V117 carry-forward identity ---------------------------------------------------------

def _v117_identity(key: str) -> str:
    """V117 (V162 body): a key whose last 10 characters parse as a date, behind a '|', strips to its identity."""
    if len(key) >= 11 and key[-11] == "|":
        try:
            date.fromisoformat(key[-10:])
            return key[:-11]
        except ValueError:
            pass
    return key


def test_v168_keys_fit_varchar_300_and_strip_to_one_identity_per_outcome():
    days = [date(2026, 3, 8) + timedelta(days=k) for k in range(3)]
    for user in ("U" * 255, "first.last.name", ""):
        for ip in ("10.0.0.1", "2001:0db8:85a3:0000:0000:8a2e:0370:7334:ffff", "(none)"):
            for failed in (False, True):
                keys = [f"SEC_NEW_ADMIN_NETWORK|{user[:200]}|{ip}{'|FAILED' if failed else ''}|{d.isoformat()}"
                        for d in days]
                assert all(len(k) <= 286 for k in keys)
                assert {_v117_identity(k) for k in keys} == {
                    f"SEC_NEW_ADMIN_NETWORK|{user[:200]}|{ip}{'|FAILED' if failed else ''}"}
    # the two outcomes of one pair never share an identity (a snooze on a failures-only event never carries over)
    assert _v117_identity("SEC_NEW_ADMIN_NETWORK|U|1.2.3.4|FAILED|2026-09-30") != \
        _v117_identity("SEC_NEW_ADMIN_NETWORK|U|1.2.3.4|2026-09-30")
    # [14]: WARN on day D and day D+1 is one identity (V117), CRIT another
    assert _v117_identity("PIPE_COPY_FAILURES|DB.S.T|WARN|2026-09-29") == \
        _v117_identity("PIPE_COPY_FAILURES|DB.S.T|WARN|2026-09-30") != \
        _v117_identity("PIPE_COPY_FAILURES|DB.S.T|CRIT|2026-09-30")


# -- RUN_NEXT PART B ------------------------------------------------------------------------------------------
_PART_B_PRESENT = ("alert scan v14 (V168:", "AS FAIL_DAY", "GROUP BY 1, 2, 3, 4", "AS SUCCESSES", "(0 successful)",
                   "LEFT(nn.USER_NAME, 200)", "Changes (Recent grant changes)", "____-__-__", "/14 rule blocks ok",
                   "SP_SCAN_ETL_CYCLE")
_PART_B_ABSENT = ("failed file load(s) (24h)", "ev.RAISED_AT >= DATEADD", "budget_usd", "alert scan v13 (V162:")


def test_v168_part_b_get_ddl_fragments(tmp_path):
    body, body162 = _body(_H), _body(_H162)
    for frag in (*_PART_B_PRESENT, *_PART_B_ABSENT):
        assert not set(frag) & {"'", "\\", "\n", "\r"}, frag
    assert all(f in body for f in _PART_B_PRESENT) and not all(f in body162 for f in _PART_B_PRESENT)
    for frag in _PART_B_ABSENT:
        assert frag not in body and frag in body162, frag
    _, part_b, _ = _extras(tmp_path)
    ddl = "GET_DDL('PROCEDURE', 'DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()')"
    for frag in _PART_B_PRESENT:
        assert f"IFF(CONTAINS({ddl}, '{frag}'), 'OK'" in part_b, frag
    for frag in _PART_B_ABSENT:
        assert f"IFF(NOT CONTAINS({ddl}, '{frag}'), 'OK'" in part_b, frag
    for check in ("'V168.1 SCHEMA_VERSION has 168'", "'alert scan 14/14 rule blocks ok'",
                  "'V168.4 stranded OPEN PERF events left to clear'"):
        assert check in part_b, check


# -- the app reads the new title shapes -------------------------------------------------------------------------

def test_v168_navigation_reads_the_new_titles():
    """R2-035: the [14] title now ends 'on <day>' and still yields its database; R2-039: the failures-only [18]
    title leads with a (dotted) user name, which sets no entity filter."""
    from app.logic import navigate
    t = navigate.investigation_target("PIPE_COPY_FAILURES", "TRXS_DW.STAGING.CLAIMS: 4 failed file load(s) on 2026-09-29")
    assert (t["page"], t["section"], t["filters"]) == ("Operations", "Pipeline SLA", {"database": "TRXS_DW"})
    failed = "first.last.name: 3 failed login attempt(s) from new network 10.1.2.3 (0 successful)"
    assert navigate.investigation_target("SEC_NEW_ADMIN_NETWORK", failed) == {
        "page": "Security", "section": "Access", "filters": {}}
