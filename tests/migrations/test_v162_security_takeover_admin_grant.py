"""Locks for V162 -- SEC_LOGIN_TAKEOVER + SEC_ADMIN_GRANT, the hourly identity alerts (Next-Fifty #39, v4.602.0).

STRUCTURE + GENERATION + LOCKS; the sqlite harness that EXECUTES the arms, the supersede sweep, the autodeclare
exclusion and the PREFLIGHT grids is tests/migrations/test_v162_harness.py. What this file proves, from the shipped
text:
  * generation -- outputs/gen_v162.py regenerates the migration byte-for-byte (LF only, nothing else written); the
    read-only PREFLIGHT (P162.1-P162.3) and the RUN_NEXT PART B are written only on request, and the PREFLIGHT
    carries the arms' own CTE chains with only the window constants moved;
  * shape -- first line, guard (-20162, v < 161), the WHEN NOT MATCHED seed, marker + SP_INCIDENT_AUTODECLARE
    BEFORE marker + SP_ALERT_SCAN, the version row; nothing runs at apply time;
  * lineage + round 13 -- both procs are re-derived from their immediately previous definers (V154 / V157), and
    removing the declared deltas gives each base body back byte-for-byte;
  * the hourly content locks (tally 14, placement outside every cadence gate, the V157 gates / sweeps / [18]
    kept, the ACCOUNT_USAGE footprint, the windows, company ALL, the explicit millisecond keys), the V117
    carry-forward non-strip property (fuzzed, with a negative control), the role / off-hours literals;
  * the RUN_NEXT PART B GET_DDL fragments;
  * the v4.602.0 app slice (playbooks, navigation, tuning, security constants, the gated captions, Admin's
    INCIDENT_AUTO_DECLARE_CRITICAL editor, RUNBOOK section 12 / 21).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests._source import ROOT, read
from tests.test_migration_proc_syntax import _strip_noise

_NAME = "V162__security_takeover_admin_grant.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V154 = read("snowflake/migrations/V154__incident_attach_automitigate.sql")
_V157 = read("snowflake/migrations/V157__alert_scan_self_watch_idle_push.sql")
_TAKE, _GRANT = "SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT"


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _body(proc_text: str) -> str:
    return proc_text[proc_text.index("$$") + 2:proc_text.rindex("$$")]


_H = _proc(_MIG, "SP_ALERT_SCAN()")
_A = _proc(_MIG, "SP_INCIDENT_AUTODECLARE()")
_H157 = _proc(_V157, "SP_ALERT_SCAN()")
_A154 = _proc(_V154, "SP_INCIDENT_AUTODECLARE()")

# ---------------------------------------------------------------------------------------------------
# Test-side copies (independent of outputs/gen_v162.py).
# ---------------------------------------------------------------------------------------------------
_MARK_AUTO = ("-- >>> derived:SP_INCIDENT_AUTODECLARE  (from V154; + no auto-declare for SEC_LOGIN_TAKEOVER / "
              "SEC_ADMIN_GRANT, V162)\n")
_MARK_SCAN = ("-- >>> derived:SP_ALERT_SCAN  (from V157; + [26] SEC_LOGIN_TAKEOVER + [27] SEC_ADMIN_GRANT hourly "
              "counting arms, tally 12 -> 14, V162)\n")
_A1 = ("          -- V162 (Next-Fifty #39, owner 2026-09-29): identity alerts never auto-declare -- a human declares after\n"
       "          -- contacting the user. [attach] below still links them to an incident a human opened for that user.\n"
       "          AND e.RULE_ID NOT IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')\n")
# review W1: [attach] links an identity alert only to an incident that already holds the SAME user
_A2_PRED = ("              AND (e.RULE_ID NOT IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')\n"
            "                   OR UPPER(SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 2))\n"
            "                      = UPPER(SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 2)))\n")
_A2 = ("              -- V162 review fix: an identity alert attaches only to an incident that already holds the\n"
       "              -- SAME user (DEDUPE_KEY field 2, upper-cased like the entity filter of SP_INCIDENT_DECLARE).\n"
       "              -- A takeover of another user stays unlinked, so it keeps its own escalation (V164) and its\n"
       "              -- own proposal, and never inherits an incident acknowledged or mitigated for someone else.\n"
       + _A2_PRED)
_ANCHOR_26 = "    -- [26] SEC_LOGIN_TAKEOVER"
_ANCHOR_27 = "    -- [27] SEC_ADMIN_GRANT"
_GATE_22 = "    IF (MOD(ct_hour, 3) = 2) THEN"
_SELF_12 = "' of 12 alert rule block(s) failed this run'"
_SELF_14 = "' of 14 alert rule block(s) failed this run'"
_RET_157 = ("'alert scan v12 (V157: + OPS_PIPELINE_DEGRADED self-watch + ETL-cycle add-on + condition-ended sweep + "
            "heartbeat, - dead break-glass arm, - retired cloud-services ratio arm, security arms every 4h, "
            "self-watch every 3h): '")
_RET_162 = "'alert scan v13 (V162: + SEC_LOGIN_TAKEOVER + SEC_ADMIN_GRANT hourly; V157 gates unchanged): '"
_FAILS_INC = "fails := fails + 1"
_ROLES = ("ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN", "ORGADMIN", "SNOW_ACCOUNTADMINS",
          "SNOW_SYSADMINS")
_SEED_TAKE = ("        ('SEC_LOGIN_TAKEOVER', 'SECURITY', 'Possible account takeover: at least THRESHOLD_NUM failed "
              "logins by one user within 15 minutes, then a successful login within 60 minutes (CRITICAL off-hours "
              "Central or for an admin-role holder)', TRUE, 'HIGH', 5, 24),\n")
_SEED_GRANT = ("        ('SEC_ADMIN_GRANT', 'SECURITY', 'Admin-tier role granted directly to a user (ACCOUNTADMIN, "
               "SECURITYADMIN, SYSADMIN, USERADMIN, ORGADMIN, SNOW_ACCOUNTADMINS, SNOW_SYSADMINS), one event per "
               "grant', TRUE, 'HIGH', 0, 24)\n")
_SEED_COLS = "(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)"

_ARM26 = _between(_H, _ANCHOR_26, _ANCHOR_27)
_ARM27 = _between(_H, _ANCHOR_27, _GATE_22)


def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PREFLIGHT_OUT", "PARTB_OUT", "V162_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v162.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


# -- generation --------------------------------------------------------------------------------------------

def test_v162_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V162_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (ROOT / "snowflake" / "migrations" / _NAME).read_bytes(), (
        "V162 drifted from its forward-generation -- edit outputs/gen_v162.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]


def _optional(tmp_path: Path) -> tuple[str, str]:
    pre, pb = tmp_path / "PREFLIGHT_V162.sql", tmp_path / "PARTB_V162.sql"
    result = _run_gen(tmp_path, V162_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pre), PARTB_OUT=str(pb))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG      # the options never change the migration
    return pre.read_text(encoding="utf-8"), pb.read_text(encoding="utf-8")


def test_v162_preflight_is_read_only_and_parses(tmp_path):
    sql, _ = _optional(tmp_path)
    assert "V162 PREFLIGHT (read-only" in sql.splitlines()[1]
    code = _strip_noise(sql)
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT",
                   "REVOKE", "EXECUTE"):
        assert not re.search(rf"\b{banned}\b", code, re.I), banned
    assert not re.search(r"(?<![:\w]):[A-Za-z_]", code), "a scripting :bind survived into the PREFLIGHT"
    assert code.count(";") == 4 and "$$" not in sql and b"\r" not in sql.encode()
    assert re.findall(r"^-- (P162\.\d) ", sql, re.M) == ["P162.1", "P162.2", "P162.3", "P162.4"]
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    parsed = sqlglot.parse(sql, dialect="snowflake")
    assert [p.key for p in parsed] == ["select"] * 4
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    for p in parsed:
        assert not [type(n).__name__ for n in p.walk() if isinstance(n, writes)]
    ctes = [c.alias for c in parsed[0].find(exp.With).expressions]
    assert ctes == ["k", "ev", "fl", "bend", "seq", "brk", "anc", "det", "adm", "x"]
    assert [c.alias for c in parsed[1].find(exp.With).expressions] == ["ag"]
    assert [c.alias for c in parsed[3].find(exp.With).expressions] == ctes


def test_v162_preflight_p162_4_is_the_first_run_crit_chain_with_the_arm_windows(tmp_path):
    """Review W17: V164's census P164.2 runs before V162's first scan, so it can never list the CRITICAL takeovers
    that scan raises -- and those escalate ~2-3h after the apply. P162.4 is arm [26]'s chain with its OWN 27h / 24h
    windows (not widened), filtered to the CRIT band, and says so."""
    sql, _ = _optional(tmp_path)
    p4 = sql[sql.index("-- P162.4 "):]
    chain = _ARM26[_ARM26.index("        ev AS ("):_ARM26.index("\n        SELECT b.RULE_ID")]
    assert chain in p4                                                      # the arm's own windows, verbatim
    assert "DATEADD('hour', -723" not in p4 and "DATEADD('hour', -720" not in p4
    assert "WHERE x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL\n" in p4           # = the arm's CRITICAL band
    band = _between(_ARM26, "               IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL, 'CRITICAL'", ",\n")
    assert "x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL" in band
    key26 = _between(_ARM26, "c.RULE_ID || '|' || LEFT(x.USER_NAME, 200)", "\n        FROM cfg c")
    assert key26.replace("c.RULE_ID", "'SEC_LOGIN_TAKEOVER'", 1) in p4
    flat = " ".join(ln.lstrip("- ") for ln in p4.splitlines() if ln.startswith("--"))
    for frag in ("V164's escalation census P164.2 cannot list them", "Read these WITH P164.2",
                 "seed ('ESCALATE_AFTER_MIN', '0') before the apply", "within 2h of the first hourly scan"):
        assert frag in flat, frag


def test_v164_p164_2_census_points_at_p162_4(tmp_path):
    """Review W17, the other half: V164's census header says what it cannot see and where that list is."""
    env = {k: v for k, v in os.environ.items() if k not in ("PREFLIGHT_OUT", "PART_B_OUT", "V164_OUT")}
    env.update(V164_OUT=str(tmp_path / "m164.sql"), PREFLIGHT_OUT=str(tmp_path / "pf164.sql"))
    result = subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v164.py")], env=env, cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m164.sql").read_bytes() == (
        ROOT / "snowflake" / "migrations" / "V164__notify_actionable_lines_escalation.sql").read_bytes()
    pf = (tmp_path / "pf164.sql").read_text(encoding="utf-8")
    head = pf[pf.index("-- P164.2 "):pf.index("WITH k AS (", pf.index("-- P164.2 "))]
    flat = " ".join(ln.lstrip("- ") for ln in head.splitlines())
    assert "NOT LISTED HERE: the CRITICAL takeovers V162's first hourly scan raises" in flat
    assert "PREFLIGHT P162.4 lists them: decide on both grids together." in flat
    dep = " ".join(line.lstrip("> ") for line in read("DEPLOYMENT.md").splitlines())
    assert "and P162.4 the CRITICAL takeovers V162's first hourly scan raises (P164.2 cannot see those" in dep


def test_v162_preflight_carries_the_arm_chains_verbatim(tmp_path):
    """What the owner previews is what the hourly scan will raise: the [26] chain ev..x and the [27] ag CTE are
    the arm text with ONLY the window constants widened (27h -> 723h read, 24h -> 720h anchors, 26h -> 720h)."""
    sql, _ = _optional(tmp_path)
    chain = _ARM26[_ARM26.index("        ev AS ("):_ARM26.index("\n        SELECT b.RULE_ID")]
    assert chain.count("DATEADD('hour', -27, CURRENT_TIMESTAMP())") == 1
    assert chain.count("DATEADD('hour', -24, CURRENT_TIMESTAMP())") == 1
    wide = (chain.replace("DATEADD('hour', -27, CURRENT_TIMESTAMP())", "DATEADD('hour', -723, CURRENT_TIMESTAMP())")
            .replace("DATEADD('hour', -24, CURRENT_TIMESTAMP())", "DATEADD('hour', -720, CURRENT_TIMESTAMP())"))
    assert wide in sql
    ag = _ARM27[_ARM27.index("        ag AS ("):_ARM27.index("\n        SELECT b.RULE_ID")]
    assert ag in sql and "WHERE g.CREATED_ON >= DATEADD('hour', -720, CURRENT_TIMESTAMP())" in sql
    # the preview key / title expressions are the arms' own, the rule id as a literal
    key26 = _between(_ARM26, "c.RULE_ID || '|' || LEFT(x.USER_NAME, 200)", "\n        FROM cfg c")
    assert key26.replace("c.RULE_ID", "'SEC_LOGIN_TAKEOVER'", 1) in sql
    key27 = _between(_ARM27, "c.RULE_ID || '|' || LEFT(g.GRANTEE_NAME, 200)", "\n        FROM cfg c")
    assert key27.replace("c.RULE_ID", "'SEC_ADMIN_GRANT'", 1) in sql
    title27 = _between(_ARM27, "LEFT('Admin role '", ", 300)")
    assert title27 in sql
    # the first-run flags are the arms' own windows; N falls back to 5 until the rule is seeded
    assert "x.TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP()) AS IN_FIRST_RUN_WINDOW" in sql
    assert "g.CREATED_ON >= DATEADD('hour', -26, CURRENT_TIMESTAMP()) AS IN_FIRST_RUN_WINDOW" in sql
    assert "GREATEST(CEIL(COALESCE(MAX(c.THRESHOLD_NUM), 5)), 2) AS N" in sql


# -- guard, order, shape -------------------------------------------------------------------------------------

def test_v162_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    guard157 = _between(_V157, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")
    want = (guard157.replace("-20157", "-20162").replace("'V157 requires V156 first", "'V162 requires V161 first")
            .replace("IF (v < 156)", "IF (v < 161)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert "EXCEPTION (-20162, 'V162 requires V161 first - apply migrations in order.');" in _MIG
    assert _MIG.count("EXECUTE IMMEDIATE") == 1
    assert "SELECT 162 AS VERSION" in _MIG
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 162);")
    head = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for word in ("WHY:", "COST", "LATENCY:", "FIRST RUN:", "ROLLBACK (order matters):",
                 "Apply AFTER V161. Idempotent; safe to re-run."):
        assert word in head, word
    # review W2/W14: the scan goes first, and V154's autodeclare comes back only AFTER the identity events are
    # closed or 24h old -- the scan-first order alone never stopped a still-open CRITICAL takeover being declared
    rb = " ".join(ln.lstrip("- ") for ln in head[head.index("-- ROLLBACK (order matters):"):
                                                  head.index("-- Apply AFTER V161.")].splitlines())
    step1, step2 = rb.index("1. Re-run V157's SP_ALERT_SCAN"), rb.index("2. Only to restore V154's SP_INCIDENT_AUTODECLARE")
    assert step1 < step2
    pre = rb[step2:]
    assert pre.index("FIRST wait 24h after step 1") < pre.index("V154 has no exclusion")
    assert "resolve every OPEN / ACK / SNOOZED SEC_LOGIN_TAKEOVER and SEC_ADMIN_GRANT event as EXPECTED" in pre
    assert "Running the scan first only keeps out the events raised between the two steps" in pre
    assert "Roll V163 back before V162" in rb
    assert "THEN\n-- optionally V154's" not in head and "Reversed, an hourly run" not in head
    # review W17: the first-run CRITICAL takeovers escalate once V164 lands; P162.4 (not P164.2) lists them
    assert "P162.4 lists them (V164's own census, P164.2, runs before they exist and cannot)" in " ".join(
        ln.lstrip("- ") for ln in head.splitlines())


def test_v162_file_order_autodeclare_before_the_scan():
    guard = _MIG.index("EXCEPTION (-20162")
    seed = _MIG.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t")
    m_auto, m_scan = _MIG.index(_MARK_AUTO), _MIG.index(_MARK_SCAN)
    c_auto = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()")
    c_scan = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()")
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < seed < m_auto < c_auto < m_scan < c_scan < version
    assert _MIG[m_auto + len(_MARK_AUTO):].startswith(
        "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()")
    assert _MIG[m_scan + len(_MARK_SCAN):].startswith("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()")


def test_v162_two_procs_one_seed_nothing_runs_at_apply():
    from tests.test_migrations_parse import _plain_statements
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 2
    assert _MIG.count("$$") == 6                                  # guard + two proc bodies
    assert "$$" not in _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "ALTER", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "UPDATE", "DELETE"):
        assert not re.search(rf"\b{banned}\b", top), banned
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 3)[:3] for s in _plain_statements(_MIG)]
    assert kinds == [["MERGE", "INTO", "DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG"],
                     ["INSERT", "INTO", "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION"]], kinds
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG)            # no tagged dollar quote
    assert "SETTINGS (KEY" not in _MIG and "INTO DBA_MAINT_DB.OVERWATCH.SETTINGS" not in _MIG
    code = _strip_noise(_MIG)
    assert not re.search(r"(?:CREATE(?:\s+OR\s+REPLACE)?\s+TASK|ALTER\s+TASK|EXECUTE\s+TASK)", code)
    assert not re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+)?(?:TABLE|VIEW|FUNCTION)", code)


def test_v162_seed_is_exact_and_when_not_matched_only():
    seed = _between(_MIG, "MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t", ";\n")
    assert seed.count(_SEED_TAKE) == 1 and seed.count(_SEED_GRANT) == 1 and seed.count("\n        ('") == 2
    assert f"    AS s{_SEED_COLS}\n" in seed and f"WHEN NOT MATCHED THEN INSERT {_SEED_COLS}" in seed
    assert "WHEN MATCHED" not in seed and "FALSE" not in seed
    for rule in (_TAKE, _GRANT):
        name = re.search(rf"\('{rule}', 'SECURITY', '([^']*)'", seed).group(1)
        assert len(name) <= 200 and ";" not in name and "$" not in name, rule
    assert len(re.findall(r"(?:MERGE INTO|UPDATE|DELETE FROM|INSERT INTO) DBA_MAINT_DB\.OVERWATCH\.ALERT_CONFIG",
                          _MIG)) == 1
    from tests.test_alert_rule_consistency import _config_enabled, _raised_rule_ids
    assert {_TAKE, _GRANT} <= _config_enabled() and {_TAKE, _GRANT} <= _raised_rule_ids()


def test_v162_description_fits_and_doubles_apostrophes():
    desc = re.search(r"SELECT 162 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc.replace("''", "") and len(desc.replace("''", "'")) <= 4000
    assert desc.startswith("Next-Fifty #39 (owner 2026-09-29)")
    assert "re-derived from V154" in desc and "re-derived from V157" in desc and "tally 12 -> 14" in desc


# -- lineage ------------------------------------------------------------------------------------------------

def test_v162_markers_name_the_current_definers():
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _migrations, _violations
    texts = _migrations()
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    for proc, base in (("SP_ALERT_SCAN", 157), ("SP_INCIDENT_AUTODECLARE", 154)):
        r = rows[(162, proc)]
        assert r["src"] == "marker" and r["claims"] == [base] and r["prev"] == base and not r["waived"], r
    defs = _definers(texts)
    assert [v for v in defs["SP_ALERT_SCAN"] if 157 <= v <= 162] == [157, 162]
    assert [v for v in defs["SP_INCIDENT_AUTODECLARE"] if 154 <= v <= 162] == [154, 162]
    assert not [v for v in _violations(texts, _HISTORICAL_WAIVERS) if v.startswith("V162 ")]
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 2


# -- round 13: normalize-and-compare -------------------------------------------------------------------------

def test_v162_hourly_normalizes_back_to_v157_byte_for_byte():
    h = _H
    i, j = h.index(_ANCHOR_26), h.index(_GATE_22)
    h = h[:i] + h[j:]                                                         # H1: arms [26] + [27]
    assert h.count(_SELF_14) == 1 and h.count("(14 - :fails)") == 5 and h.count("'/14 rule blocks ok'") == 3
    assert h.count(_RET_162) == 1
    h = h.replace(_SELF_14, _SELF_12)                                          # H2
    h = h.replace("(14 - :fails)", "(12 - :fails)").replace("'/14 rule blocks ok'", "'/12 rule blocks ok'")   # H3
    h = h.replace(_RET_162, _RET_157)                                          # H4
    assert h == _H157


def test_v162_autodeclare_normalizes_back_to_v154_byte_for_byte():
    assert _A.count(_A1) == 1 and _A.count(_A2) == 1
    # A1 sits inside the crit CTE, directly before its closing paren
    assert _A.count(_A1 + "    )\n    SELECT UUID_STRING() AS INCIDENT_ID, FAMILY, COMPANY,\n") == 1
    # A2 is [attach]'s last WHERE predicate: after the NOT EXISTS m2 line, before the QUALIFY (filters, not ranks)
    assert _A.count("                              WHERE m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID)\n"
                    + _A2 + "            QUALIFY ROW_NUMBER() OVER (\n") == 1
    assert _A.replace(_A1, "").replace(_A2, "") == _A154
    assert _A.replace(_A1, "") != _A154 and _A.replace(_A2, "") != _A154          # both deltas are real


def test_v162_exclusion_lives_in_the_crit_cte_and_the_same_user_rule_in_attach():
    crit = _between(_A, "    WITH crit AS (", "    SELECT UUID_STRING()")
    attach = _between(_A, "    -- [attach] V154", "    -- [auto-mitigate] V154")
    mitigate = _between(_A, "    -- [auto-mitigate] V154", "SELECT COUNT(*) INTO :made")
    needle = "e.RULE_ID NOT IN ("
    assert _A.count(needle) == 2 and crit.count(needle) == 1 and attach.count(needle) == 1
    assert needle not in mitigate
    assert "AND e.RULE_ID NOT IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')\n" in crit
    # review W1: in [attach] the identity rules are gated on the SAME user, never excluded outright
    assert _A2_PRED in attach and "AND e.RULE_ID NOT IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT')\n" not in attach
    assert attach.index(_A2_PRED) < attach.index("            QUALIFY ROW_NUMBER() OVER (")
    # the member INSERT joins _OW_AUTODECL (the excluded families never reach it) and is unchanged
    assert _between(_A, "    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS", "    -- [attach] V154") == \
        _between(_A154, "    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS", "    -- [attach] V154")
    # the toggle still returns first
    assert _A.index("RETURN 'auto-declare off';") < _A.index("    WITH crit AS (")


# -- hourly content locks -------------------------------------------------------------------------------------

def test_v162_tallies_equal_the_counting_arms():
    from tests.test_alert_rule_consistency import _FAILS_INC_RE, _SCAN_DENOMINATORS
    assert len(_FAILS_INC_RE.findall(_H)) == 14 and len(_FAILS_INC_RE.findall(_H157)) == 12
    self_alert_re, return_re = _SCAN_DENOMINATORS["SP_ALERT_SCAN"]
    assert {int(x) for x in re.findall(self_alert_re, _H)} == {14}
    assert re.findall(return_re, _H) == [("14", "14")] * 3                      # [hb] STATUS x2 + RETURN
    # the two [hb] ROW_COUNT sites the global denominator guard does not read
    assert _H.count("               ROW_COUNT = (14 - :fails),\n") == 1
    assert _H.count("                   (14 - :fails), 1, 'alert scan ' || (14 - :fails) || '/14 rule blocks ok';\n") == 1
    assert "(12 - :fails)" not in _H and "/12 rule blocks ok" not in _H and "alert scan v12" not in _H
    for arm, rule in ((_ARM26, _TAKE), (_ARM27, _GRANT)):
        assert arm.count(_FAILS_INC) == 1 and arm.count("    BEGIN\n") == 1 and arm.count("    EXCEPTION\n") == 1
        assert arm.endswith("            SELECT 'AlertScan', 'rule_block_failed', :emsg,\n"
                            f"                   'rule {rule} - other rules unaffected', CURRENT_ROLE();\n    END;\n")


def _gate_spans(body: str) -> list[tuple[int, int]]:
    opens = [m.start() for m in re.finditer(r"^    IF \(MOD\(ct_hour, \d\) = \d\) THEN", body, re.M)]
    closes = [m.end() for m in re.finditer(r"^    END IF;   -- /V157 cadence gate:[^\n]*\n", body, re.M)]
    assert len(opens) == len(closes) == 5
    return list(zip(opens, closes, strict=True))


def test_v162_arms_are_ungated_after_21_and_before_the_sweeps():
    i26, i27 = _H.index(_ANCHOR_26), _H.index(_ANCHOR_27)
    end27 = i27 + len(_ARM27)
    assert _H[:i26].endswith("                   'rule posture-metric (generic) - other rules unaffected', "
                             "CURRENT_ROLE();\n    END;\n")                                  # after [21]'s END
    assert _H[end27:].startswith(_GATE_22)                                                 # right before the [22] gate
    for a, b in _gate_spans(_H):
        assert not (a <= i26 < b) and not (a <= end27 - 1 < b), "a new arm sits inside a cadence gate"
    assert end27 < _H.index("    IF (fails > 0) THEN") < _H.index("    -- V067 #40: supersede")
    assert _H.index("    -- V067 #40: supersede") < _H.index("    -- [snooze carry-forward sweep] V117")


def test_v162_keeps_the_v157_carry_overs():
    assert _H.count("IF (MOD(ct_hour, 4) = 1) THEN") == 4 and _H.count("IF (MOD(ct_hour, 3) = 2) THEN") == 1
    assert "        SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())) INTO :ct_hour;\n" in _H
    for pair in ("REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')", "REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|')",
                 "REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')", "TRY_TO_DATE(RIGHT(s.DEDUPE_KEY, 10))",
                 "CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE();", "'ALERT_SCAN_HOURLY heartbeat stamp - alerts unaffected'",
                 "V157: only rules whose still-firing set this sweep recomputes"):
        assert pair in _H, pair
    # [18] still names exactly its three roles
    arm18 = _between(_H, "    -- [18] SEC_NEW_ADMIN_NETWORK", "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]")
    assert arm18 == _between(_H157, "    -- [18] SEC_NEW_ADMIN_NETWORK",
                             "    IF (MOD(ct_hour, 4) = 1) THEN   -- V157 cadence gate: [20]")
    assert "AND ROLE IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')" in arm18


def test_v162_account_usage_footprint_windows_and_keys():
    body = _body(_H)
    assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", body)) == {
        "COPY_HISTORY", "CREDENTIALS", "GRANTS_TO_ROLES", "GRANTS_TO_USERS", "LOGIN_HISTORY"}
    assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _ARM26)) == {"LOGIN_HISTORY", "GRANTS_TO_USERS"}
    assert set(re.findall(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)", _ARM27)) == {"GRANTS_TO_USERS"}
    assert _ARM26.count("WHERE EVENT_TIMESTAMP >= DATEADD('hour', -27, CURRENT_TIMESTAMP())") == 1
    assert _ARM26.count("AND TS >= DATEADD('hour', -24, CURRENT_TIMESTAMP())") == 1
    assert _ARM27.count("AND g.CREATED_ON >= DATEADD('hour', -26, CURRENT_TIMESTAMP())") == 1
    key26 = _between(_ARM26, "               c.RULE_ID || '|' || LEFT(x.USER_NAME, 200)", "\n        FROM cfg c")
    key27 = _between(_ARM27, "               c.RULE_ID || '|' || LEFT(g.GRANTEE_NAME, 200)", "\n        FROM cfg c")
    for key in (key26, key27):
        assert "CURRENT_DATE" not in key and "CURRENT_TIMESTAMP" not in key and "EVENT_ID" not in key
        assert key.rstrip().endswith(", 'YYYY-MM-DD HH24:MI:SS.FF3')")
    assert "TO_VARCHAR(CONVERT_TIMEZONE('UTC', x.TS)::TIMESTAMP_NTZ, 'YYYY-MM-DD HH24:MI:SS.FF3')" in key26
    assert "IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL, 'CRIT', 'WARN')" in key26
    assert "TO_VARCHAR(g.CREATED_CT, 'YYYY-MM-DD HH24:MI:SS.FF3')" in key27 and "g.ROLE" in key27
    for arm in (_ARM26, _ARM27):
        assert "COMPANY_FOR_USER" not in arm and "        SELECT c.RULE_ID,\n               'ALL',\n" in arm
        assert "LEFT('" in arm and ", 300),\n" in arm and ", 2000),\n" in arm      # TITLE / DETAIL bounded
    # bend is a linear equi-join on the failure number, never a range self-join
    assert "AND f1.RN = f2.RN - (k.N - 1)" in _ARM26
    assert "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS LAST_BEND" in _ARM26
    assert "GREATEST(CEIL(COALESCE(MAX(THRESHOLD_NUM), 5)), 2) AS N" in _ARM26 and "HAVING COUNT(*) > 0" in _ARM26
    assert "IFF(x.OFF_HOURS OR x.ADMIN_ROLE IS NOT NULL, 'CRITICAL', c.SEVERITY)" in _ARM26
    assert "               c.SEVERITY,\n" in _ARM27 and "'CRITICAL'" not in _ARM27          # flat rule severity
    assert "               1,\n" in _ARM27                                                  # METRIC_VALUE constant
    assert "never auto-declares" in _ARM26.lower() and "never auto-declares" in _ARM27.lower()


def _role_list(arm: str) -> list[str]:
    (inlist,) = re.findall(r"ROLE IN \(([^)]*)\)", arm)
    return re.findall(r"'(\w+)'", inlist)


def test_v162_role_list_and_off_hours_literals_match_the_app_constants():
    from app.data import security_sql
    assert tuple(_role_list(_ARM26)) == tuple(_role_list(_ARM27)) == security_sql.ALERT_ADMIN_ROLES == _ROLES
    (lo_hour,) = {int(x) for x in re.findall(r"HOUR\(\w\.\w+\) >= (\d+)", _ARM26 + _ARM27)}
    (hi_hour,) = {int(x) for x in re.findall(r"HOUR\(\w\.\w+\) < (\d+)", _ARM26 + _ARM27)}
    (wk,) = {int(x) for x in re.findall(r"DAYOFWEEKISO\(\w\.\w+\) >= (\d+)", _ARM26 + _ARM27)}
    assert (lo_hour, hi_hour) == (security_sql.OFF_HOURS_START_HOUR, security_sql.OFF_HOURS_END_HOUR) == (20, 6)
    assert tuple(range(wk, 8)) == security_sql.OFF_HOURS_WEEKEND_ISO == (6, 7)
    assert "(HOUR(a.TS_CT) >= 20 OR HOUR(a.TS_CT) < 6 OR DAYOFWEEKISO(a.TS_CT) >= 6) AS OFF_HOURS" in _ARM26
    assert "IFF(HOUR(g.CREATED_CT) >= 20 OR HOUR(g.CREATED_CT) < 6 OR DAYOFWEEKISO(g.CREATED_CT) >= 6," in _ARM27
    # the clock is the event's own Central wall clock, never the run's
    assert "CONVERT_TIMEZONE('America/Chicago', TS)::TIMESTAMP_NTZ AS TS_CT" in _ARM26
    assert "CONVERT_TIMEZONE('America/Chicago', CREATED_ON)::TIMESTAMP_NTZ AS CREATED_CT" in _ARM27


def test_v162_arm_statements_parse():
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    for arm in (_ARM26, _ARM27):
        stmt = arm[arm.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):arm.index(";\n    EXCEPTION")]
        (parsed,) = sqlglot.parse(stmt, dialect="snowflake")
        assert [c.name for c in parsed.this.expressions] == ["RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL",
                                                             "METRIC_VALUE", "DEDUPE_KEY"]
        (b,) = [s for s in parsed.find_all(exp.Subquery) if s.alias == "b"]
        assert len(b.this.expressions) == 7
    crit = _A[_A.index("    WITH crit AS ("):_A.index("    GROUP BY FAMILY, COMPANY;") + len("    GROUP BY FAMILY, COMPANY")]
    sqlglot.parse(crit, dialect="snowflake")
    from tests.test_migrations_parse import _plain_statements
    for plain in _plain_statements(_MIG):
        sqlglot.parse(plain, dialect="snowflake")


# -- V117 carry-forward non-strip property (fuzzed) ------------------------------------------------------------
_CT, _UTC = ZoneInfo("America/Chicago"), ZoneInfo("UTC")


def _try_to_date(s: str) -> bool:
    """A CONSERVATIVE mirror of Snowflake's TRY_TO_DATE on a 10-character string: an ISO date, a US or dash date,
    or an integer (TRY_TO_DATE reads an integer string as epoch seconds) all count as dates."""
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|\d{2}-[A-Za-z]{3}-\d{4}|-?\d{1,10}", s))


def _v117_identity(key: str) -> str:
    """V157:1067-1072 -- the band-independent identity the snooze carry-forward compares."""
    return key[:-11] if len(key) >= 11 and key[-11] == "|" and _try_to_date(key[-10:]) else key


def _key26(user: str, band: str, ts_ct: datetime) -> str:
    utc = ts_ct.replace(tzinfo=_CT).astimezone(_UTC)
    return f"{_TAKE}|{user[:200]}|{band}|{utc:%Y-%m-%d %H:%M:%S}.{utc.microsecond // 1000:03d}"


def _key27(user: str, role: str, created_ct: datetime) -> str:
    return f"{_GRANT}|{user[:200]}|{role}|{created_ct:%Y-%m-%d %H:%M:%S}.{created_ct.microsecond // 1000:03d}"


def test_v162_keys_never_look_date_banded_to_the_v117_carry_forward():
    assert "IFF(SUBSTR(s.DEDUPE_KEY, -11, 1) = '|'\n                     AND TRY_TO_DATE(RIGHT(s.DEDUPE_KEY, 10)) IS NOT NULL" in _H
    # teeth: the mirror strips a real date tail and the bare EVENT_ID tail the spec rejected
    assert _v117_identity("COST_AI|U|2026-09-29") == "COST_AI|U"
    assert _v117_identity(f"{_TAKE}|U|WARN|1234567890") == f"{_TAKE}|U|WARN"
    day = datetime(2026, 11, 1)                                        # the fall-back DST day, every 7 minutes
    stamps = [day + timedelta(minutes=7 * i, milliseconds=i % 1000) for i in range(24 * 60 // 7 + 2)]
    stamps += [datetime(2026, 3, 8, 3, 0), datetime(2026, 12, 31, 23, 59, 59, 999_000)]
    for n in range(201):
        user = ("A.B" * 70)[:n]
        for ts in stamps[::max(1, n % 9 + 1)]:
            for key in (_key26(user, "WARN", ts), _key26(user, "CRIT", ts), _key27(user, "SNOW_SYSADMINS", ts)):
                assert _v117_identity(key) == key, key
                assert len(key) <= 300
    # a 255-character name is cut to 200 by LEFT, so the longest keys still fit VARCHAR(300)
    assert len(_key27("x" * 255, "SNOW_ACCOUNTADMINS", day)) == 259
    assert len(_key26("x" * 255, "WARN", day)) == 248


# -- RUN_NEXT PART B -------------------------------------------------------------------------------------------
# CONTAINS(GET_DDL('PROCEDURE', ...), '<frag>'): each fragment must literally be in the body it is checked
# against (GET_DDL returns the stored body, comments included) and stay free of quotes, backslashes and newlines.
_PART_B = {
    "SP_ALERT_SCAN()": (("SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT", "alert scan v13 (V162:", "/14 rule blocks ok",
                         "LAST_BEND", "HH24:MI:SS.FF3", "IF (MOD(ct_hour, 4) = 1) THEN", "SP_SCAN_ETL_CYCLE"),
                        ("/12 rule blocks ok",)),
    "SP_INCIDENT_AUTODECLARE()": (("e.RULE_ID NOT IN (", "V162 (Next-Fifty #39",
                                   "OR UPPER(SPLIT_PART(COALESCE(a.DEDUPE_KEY", "incident_attach_failed",
                                   "incident_mitigate_failed"), ()),
}


def test_v162_part_b_fragments(tmp_path):
    _, partb = _optional(tmp_path)
    bodies = {"SP_ALERT_SCAN()": (_body(_H), _body(_H157)), "SP_INCIDENT_AUTODECLARE()": (_body(_A), _body(_A154))}
    for proc, (present, absent) in _PART_B.items():
        new, base = bodies[proc]
        for frag in (*present, *absent):
            assert not set(frag) & {"'", "\\", "\n", "\r"}, (proc, frag)
            assert f"'DBA_MAINT_DB.OVERWATCH.{proc}'), '{frag}')" in partb, (proc, frag)   # the grid checks it
        assert all(f in new for f in present), proc
        assert not all(f in base for f in present), proc                    # the grid tells V162 from its base
        for frag in absent:
            assert frag not in new and frag in base, (proc, frag)
    code = _strip_noise(partb)
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP"):
        assert not re.search(rf"\b{banned}\b", code), banned              # the optional autodeclare CALL is commented
    assert "-- CALL DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE();" in partb
    assert not re.search(r"SP_ALERT_SCAN(?:_DAILY)?\(\);", code) and "SP_NOTIFY_WEBHOOK" not in partb


# -- app slice (v4.602.0) ---------------------------------------------------------------------------------------

def test_v162_playbooks():
    from app.logic.playbooks import PLAYBOOKS, playbook_for
    from tests.test_alert_rule_consistency import _RETIRED_RE
    for rule in (_TAKE, _GRANT):
        pb = playbook_for(rule)
        assert rule in PLAYBOOKS and pb == PLAYBOOKS[rule] and pb.startswith("**Means:**"), rule
        assert "$" not in re.sub(r"`[^`]*`", "", pb), rule
        assert not _RETIRED_RE.search(pb), rule
        assert "never auto-declare" in pb and "Cadence (V162): checked every hour" in pb, rule
        assert all(role in pb for role in _ROLES), rule
    take, grant = PLAYBOOKS[_TAKE], PLAYBOOKS[_GRANT]
    assert "20:00-06:00 Central" in take and "15 minutes" in take and "60 minutes" in take
    assert "*Account-takeover candidates*" in take and "`ALTER USER <USER> SET DISABLED = TRUE;`" in take
    assert "resolve as EXPECTED" in take and "declare an incident" in take
    assert "Security > Changes → *Recent grant changes*" in grant and "`REVOKE ROLE <ROLE> FROM USER <USER>;`" in grant
    assert "already revoked" in grant and "first grant" in grant and "off-hours" in grant
    # the order: right after SEC_NEW_ADMIN_NETWORK
    keys = list(PLAYBOOKS)
    assert keys[keys.index("SEC_NEW_ADMIN_NETWORK") + 1:keys.index("SEC_NEW_ADMIN_NETWORK") + 3] == [_TAKE, _GRANT]
    # OPS_SCAN_DEGRADED names the hourly tally V162 ships (and, review W18, the tally before V162 is applied)
    osd = PLAYBOOKS["OPS_SCAN_DEGRADED"]
    assert "still reports 14/14 ok (12/12 before V162)" in osd and osd.count("12/12") == 1
    assert "COST_SLEEP_POLLING" in osd
    # review W13: the lens lists the alert's user only under company ALL (the event is account-wide, the lens is
    # company-scoped), a covering window and a threshold of 5+ -- never "every alert shows there"
    assert "every alert of this rule shows there" not in take
    for frag in ("a wider 6-hour lens at 5+ failures and follows the top-bar company filter",
                 "this event is account-wide (company ALL)", "the rule threshold is 5 or more",
                 "A user missing from it proves nothing until the company is set to ALL."):
        assert frag in take, frag
    # review W18: SEC_FAILED_LOGINS points at the takeover rule only while it is enabled (and V162 applied); the
    # lens is the check, a missing event rules nothing out
    failed = PLAYBOOKS["SEC_FAILED_LOGINS"]
    assert "check for that event first" not in failed
    for frag in ("since V163 the title says", "Since V162, and only while SEC_LOGIN_TAKEOVER is enabled in Alerts > "
                 "Rules", "*Account-takeover candidates* (step 1) shows that whatever else is set up",
                 "a missing event does not rule a breakthrough out", "set the top-bar company to ALL"):
        assert frag in failed, frag


def test_v162_navigation_sets_no_entity_filter_for_user_text():
    from app.logic import navigate
    assert navigate._RULE_TARGETS[_GRANT] == ("Security", "Changes")
    assert _TAKE not in navigate._RULE_TARGETS                                      # the SEC family default
    assert {_TAKE, _GRANT} <= navigate._NO_ENTITY_FILTER_RULES
    text = ("Possible account takeover: first.last.name logged in 12 min after a burst of 7 failed logins "
            "(off-hours) Success from 203.0.113.9 ... Sample error: WH_X DBA_MAINT_DB.OVERWATCH.X")
    assert navigate.investigation_target(_TAKE, text) == {"page": "Security", "section": "Access", "filters": {}}
    assert navigate.investigation_target(_GRANT, "Admin role SYSADMIN granted to jane.doe.corp.admin WH_Y") == {
        "page": "Security", "section": "Changes", "filters": {}}
    from app.logic.alert_evidence import plan_for_alert
    for rule in (_TAKE, _GRANT):
        assert navigate.fix_target(rule, text) is None and navigate.inline_fix_warehouse(rule, text) == ""
        assert rule not in navigate.FIX_TARGETS and rule not in navigate.INLINE_FIX_RULES
        assert plan_for_alert(rule, text, "", "2026-09-30") is None              # no query-evidence pack
    # the carve-out is rule-scoped: an entity rule still reads the same text
    assert navigate.investigation_target("PERF_SPILL_GB", text)["filters"] == {
        "warehouse_contains": "WH_X", "database": "FIRST"}


def test_v162_tuning_never_suggests_a_threshold_for_admin_grants():
    import pandas as pd

    from app.logic import tuning
    assert frozenset({_GRANT}) == tuning.NO_THRESHOLD_RULES
    ev = pd.DataFrame({"METRIC_VALUE": [1.0] * 12, "RESOLUTION_KIND": ["NOISE"] * 10 + ["ACTIONED"] * 2})
    got = tuning.suggest_threshold(ev, 0.0, rule_id=_GRANT)
    assert got == {"ok": False, "basis": "Raised once per grant; this rule has no threshold.", "noise_n": 10,
                   "actioned_n": 2}
    assert tuning.suggest_threshold(None, 0.0, rule_id="sec_admin_grant ")["ok"] is False
    assert tuning.suggest_threshold(ev, 0.0, rule_id=_TAKE)["basis"] != tuning.NO_THRESHOLD_BASIS   # tunes normally
    table = tuning.suggestions_by_rule(ev.assign(RULE_ID=_GRANT), {_GRANT: 0.0})
    assert pd.isna(table.loc[0, "SUGGESTED_THRESHOLD"]) and table.loc[0, "NOISE_N"] == 10


def test_v162_captions_are_schema_gated():
    sec = read("app/ui/pages/security.py")
    cap = sec[sec.index("if has_migration(162, _PAGE):"):]
    cap = cap[:cap.index("st.caption(_toggle_cost_hint(\"takeover\"))")]
    assert "raises SEC_LOGIN_TAKEOVER for the stricter case" in cap and "wider 6-hour lens" in cap
    assert sec.count("raises SEC_LOGIN_TAKEOVER") == 1
    # review W13: no unconditional "lists its user here" -- the lens is company-scoped and fixed at 5 failures
    flat = re.sub(r'"\s*\n\s*"', "", cap)
    assert "so a window that includes such a login lists its user here" not in flat
    for frag in ("That alert is account-wide but this table follows the company filter",
                 "a rule threshold of 5 or more", "set the company to ALL before reading a missing user as a false alarm"):
        assert frag in flat, frag
    assert 'Account-takeover candidates (failed burst → success)", ""' in sec              # label kept
    assert sec.index("section_header(\"Account-takeover candidates") < sec.index("if has_migration(162, _PAGE):")
    cr = read("app/ui/pages/control_room.py")
    assert ('_id_txt = (" Account-takeover and admin-grant alerts never auto-declare; declare them by hand."\n'
            '                   if has_migration(162, _PAGE) else "")') in cr
    assert "unless INCIDENT_AUTO_DECLARE_CRITICAL is off in Settings.\" + _loop_txt + _id_txt)" in cr
    assert cr.count("never auto-declare") == 1


def test_v162_admin_lists_the_autodeclare_switch_as_an_editable_setting():
    from app.config import DEFAULT_SETTINGS
    from app.ui.pages import admin
    assert DEFAULT_SETTINGS["INCIDENT_AUTO_DECLARE_CRITICAL"] == "TRUE"
    assert admin._SETTING_EDITORS["INCIDENT_AUTO_DECLARE_CRITICAL"] == ("enum", ["TRUE", "FALSE"])
    v032 = read("snowflake/migrations/V032__incident_object.sql")
    assert "'INCIDENT_AUTO_DECLARE_CRITICAL' AS KEY, 'TRUE' AS VALUE" in v032          # seeded (test_v093 guard)
    assert "SELECT COALESCE(MAX(VALUE), 'TRUE') INTO :enabled" in _A                   # still read hourly


def test_v162_runbook():
    rb = read("RUNBOOK.md")
    rows = {rule: [ln for ln in rb.splitlines() if ln.startswith(f"| {rule} | SECURITY |")] for rule in (_TAKE, _GRANT)}
    (take,), (grant,) = rows[_TAKE], rows[_GRANT]
    assert "hourly [26], V162" in take and "never auto-declares an incident" in take and "20:00-06:00 Central" in take
    assert "hourly [27], V162" in grant and "never auto-declares" in grant and "flat HIGH" in grant
    flat = " ".join(rb.split())
    assert "counts as ok in the 14-block tally" in flat and "still reports 14/14 ok" in flat
    assert "[26] SEC_LOGIN_TAKEOVER and [27] SEC_ADMIN_GRANT are ungated" in flat
    sop = rb[rb.index("## §21 Incidents"):]
    assert "SP_INCIDENT_AUTODECLARE skips SEC_LOGIN_TAKEOVER and SEC_ADMIN_GRANT" in " ".join(sop.split())
    # review W1: the SOP names the same-user rule for [attach]
    assert "only when that incident already holds the SAME user" in " ".join(sop.split())


def test_v162_runbook_rollback_clears_the_identity_events_before_v154_returns():
    """Review W2/W14/W18: scan first; V154's autodeclare only after a 24h wait or the EXPECTED data step (SNOOZED
    included), stated BEFORE the re-run; V163 rolls back before V162; the wave paragraph no longer promises that no
    rollback needs a data change, nor that V162 rolls back on its own."""
    rb = read("RUNBOOK.md")
    wave = rb[rb.index("**Rolling back wave 4 (V162-V165).**"):].split("\n\n", 1)[0]
    assert "None of these rollbacks needs a data change" not in wave and "each rolls back on its own" not in wave
    assert "V162 does not: roll V163 back before it" in wave and "a 24-hour wait or a data step first" in wave
    sec = rb[rb.index("**Rolling back V162 (order matters).**"):rb.index("| Rule | Family | Fires when")]
    flat = " ".join(sec.split())
    assert flat.index("Roll V163 back first") < flat.index("1. Re-run V157's")
    step2 = flat[flat.index("2. Only if V154's"):]
    data = ("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED', "
            "RESOLVED_AT = CURRENT_TIMESTAMP() WHERE RULE_ID IN ('SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT') AND "
            "STATUS IN ('OPEN', 'ACK', 'SNOOZED');")
    assert step2.index("wait at least 24 hours after step 1") < step2.index(data) < step2.index(
        "Then re-run V154's procedure")
    assert "Running the scan first only keeps out the events raised between the two steps" in step2
    assert "Reversed, an hourly TASK_INCIDENT_AUTODECLARE" not in flat
    # the data step is valid SQL on the V004 columns (sqlglot, when available)
    sqlglot = pytest.importorskip("sqlglot")
    (stmt,) = sqlglot.parse(data, dialect="snowflake")
    assert stmt.key == "update"


# -- integration lockstep (validate / docs / Admin). Completed by the wave-4 integrator: snowflake/validate.sql,
#    DEPLOYMENT.md, README.md and admin._EXPECTED_MIGRATIONS are shared files a single slice does not edit.

def test_validate_and_docs_track_v162():
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert f"snowflake/migrations/{_NAME}" in read(rel), rel


def test_v162_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 162 in _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[162])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text and not text.endswith(".")
