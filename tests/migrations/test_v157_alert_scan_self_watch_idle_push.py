"""Locks for V157 — the single wave-2 re-derivation of both alert scans (Next-Fifty wave 2b).

Ranks 10a/b/d + 13 + 2 (the ETL-cycle add-on slot) + 12c, merged onto the CURRENT definer of both procs,
V141. Hourly SP_ALERT_SCAN: the dead arm [15] out; [22] OPS_PIPELINE_DEGRADED + [23] PIPE_ETL_CYCLE add-on
in; the V091 auto-clear sweep scoped to its 3 PERF rules; a #12c condition-ended sweep for SEC_CRED_EXPIRY /
SEC_NEW_EXPOSURE; the arm [10] recurrence fix; an [hb] heartbeat. Daily SP_ALERT_SCAN_DAILY: [22] (byte-
identical), [24] COST_IDLE_OPPORTUNITY, [hb]. Tallies 13 -> 13 and 9 -> 11.

Byte-locked to outputs/gen_v157.py; the normalize-and-compare locks prove nothing else in either V141 body
moved (the round-13 class: a re-derivation silently dropping an intervening version's change).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIGDIR = _ROOT / "snowflake" / "migrations"
_NAME = "V157__alert_scan_self_watch_idle_push.sql"
_MIG = (_MIGDIR / _NAME).read_text(encoding="utf-8")
_V141 = (_MIGDIR / "V141__alert_cadence_daily_cost_rules.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


_H = _proc(_MIG, "SP_ALERT_SCAN()")
_D = _proc(_MIG, "SP_ALERT_SCAN_DAILY()")
_H141 = _proc(_V141, "SP_ALERT_SCAN()")
_D141 = _proc(_V141, "SP_ALERT_SCAN_DAILY()")

_ARM22_H = _between(_H, "    -- [22] OPS_PIPELINE_DEGRADED", "    -- [23] PIPE_ETL_CYCLE")
_ARM22_D = _between(_D, "    -- [22] OPS_PIPELINE_DEGRADED", "    -- [24] COST_IDLE_OPPORTUNITY")
_ARM23 = _between(_H, "    -- [23] PIPE_ETL_CYCLE", "    IF (fails > 0) THEN\n")
_ARM24 = _between(_D, "    -- [24] COST_IDLE_OPPORTUNITY", "    -- [17] PIPE_REF_GAP")
_ARM10 = _between(_H, "    -- [10] SEC_CRED_EXPIRY", "    -- [11] COST_CLOUD_SVC_RATIO")
_ARM20 = _between(_H, "    -- [20] SEC_NEW_EXPOSURE", "    -- [21] SEC_POSTURE_METRIC")
_V091 = _between(_H, "    -- [auto-clear sweep] V091:", "    -- [condition-ended sweep]")
_CE = _between(_H, "    -- [condition-ended sweep]", "\n    -- [snooze carry-forward sweep] V117:")
_HB_H = _between(_H, "    -- [hb] scan heartbeat", "    RETURN ")
_HB_D = _between(_D, "    -- [hb] scan heartbeat", "    RETURN ")

_ARM15_V141 = _between(_H141, "    -- [15] SEC_BREAK_GLASS_USE\n", "    -- [17] COST_DEPT_BUDGET_PACE\n")
_RET_H141 = "    RETURN 'alert scan v11 (V091: + auto-clear sweep): ' || (13 - :fails) || '/13 rule blocks ok';\n"
_RET_D141 = ("    RETURN 'alert scan daily v2 (V141: storage-surge/serverless-creep/egress-spike moved off the hourly "
             "scan): ' || (9 - :fails) || '/9 rule blocks ok (daily)';\n")
_C1_LINE = ("           AND ev.RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')   "
            "-- V157: only rules whose still-firing set this sweep recomputes; other opt-ins have their own "
            "clear sweep\n")
_H2_V141 = "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n        );\n"
_H2_NEW = (
    "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
    "              AND COALESCE(e.RESOLUTION_KIND, '') NOT IN ('CONDITION_ENDED', 'SUPERSEDED')   "
    "-- V157: a rotated credential's next expiry cycle re-alerts (key has no date)\n"
    "        )\n"
    "          -- V157: never mint EXPIRING while this credential's EXPIRED event is live (the supersede sweep "
    "would resolve it in the same run: hourly churn)\n"
    "          AND NOT EXISTS (\n"
    "            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS h\n"
    "            WHERE b.DEDUPE_KEY LIKE '%|EXPIRING'\n"
    "              AND h.RULE_ID = b.RULE_ID\n"
    "              AND h.DEDUPE_KEY = REPLACE(b.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')\n"
    "              AND h.STATUS IN ('OPEN', 'ACK', 'SNOOZED')\n"
    "        );\n"
)
_FAILS_INC = "fails := fails + 1"


# -- generation ------------------------------------------------------------------------------------

def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PREFLIGHT_OUT"}
    env.update(extra)
    return env


def test_v157_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v157.py")],
                            env=_gen_env(V157_OUT=str(output)), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _MIG, (
        "V157 drifted from its forward-generation — edit outputs/gen_v157.py, not the .sql.")
    assert "PREFLIGHT" not in result.stdout          # the preflight is written only on request


def test_v157_preflight_is_the_arm24_chain_as_a_read_only_select(tmp_path):
    """PREFLIGHT_OUT (owner runbox file) is generated from the SAME [24] text, so it cannot drift."""
    pre = tmp_path / "PREFLIGHT_WAVE2B.sql"
    result = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v157.py")],
                            env=_gen_env(V157_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pre)),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    sql = pre.read_text(encoding="utf-8")
    body = "\n".join(ln for ln in sql.splitlines() if not ln.startswith("--"))
    assert body.lstrip().startswith("WITH px AS (") and body.rstrip().endswith("ORDER BY o.MONTHLY_USD DESC;")
    for banned in ("INSERT ", "UPDATE ", "MERGE ", "DELETE ", "CALL ", "CREATE ", "ALTER ", "DROP "):
        assert banned not in body.upper(), banned
    # every CTE line of the arm's clk -> opp chain is carried verbatim (8 spaces dedented)
    chain = _ARM24[_ARM24.index("        clk AS (\n"):_ARM24.index("        SELECT b.RULE_ID")]
    for line in chain.replace(":credit_price", "(SELECT CREDIT_PRICE FROM px)").splitlines():
        assert line[8:] in sql, line
    assert "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY" in sql
    sqlglot = pytest.importorskip("sqlglot")
    parsed = sqlglot.parse(sql, dialect="snowflake")
    assert len(parsed) == 1 and parsed[0].key == "select"


# -- guard, order, shape ---------------------------------------------------------------------------

def test_v157_guarded_and_versioned():
    assert "not_ready EXCEPTION (-20157, 'V157 requires V156 first - apply migrations in order.');" in _MIG
    assert "IF (v < 156) THEN" in _MIG
    assert "SELECT 157 AS VERSION" in _MIG and "WHERE VERSION = 157)" in _MIG
    assert _MIG.startswith(f"-- {_NAME}\n")


def test_v157_file_order():
    guard = _MIG.index("EXCEPTION (-20157")
    seed = _MIG.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t")
    mark_h = _MIG.index("-- >>> derived:SP_ALERT_SCAN  (from V141;")
    create_h = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()")
    mark_d = _MIG.index("-- >>> derived:SP_ALERT_SCAN_DAILY  (from V141;")
    create_d = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()")
    opt_in = _MIG.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n   SET AUTO_CLEAR_ENABLED = TRUE")
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < seed < mark_h < create_h < mark_d < create_d < opt_in < version


def test_v157_is_two_procs_one_seed_one_opt_in_no_task_no_top_level_call():
    from tests.test_migrations_parse import _plain_statements
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 2
    top = "".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0)
    for banned in ("CREATE TASK", "ALTER TASK", "EXECUTE TASK", "CALL ", "CREATE OR REPLACE VIEW",
                   "CREATE TABLE", "ALTER TABLE", "DROP "):
        assert banned not in top, banned
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 2)[:2] for s in _plain_statements(_MIG)]
    assert kinds == [["MERGE", "INTO"], ["UPDATE", "DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG"],
                     ["INSERT", "INTO"]], kinds
    # the only CALL anywhere is the [23] add-on inside the hourly body (no raiser runs at apply time)
    assert _MIG.count("CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE()") == 1
    assert "SP_SCAN_ETL_CYCLE" not in _D


def test_v157_seed_is_when_not_matched_only():
    seed = _between(_MIG, "MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t", ";\n")
    assert "WHEN MATCHED" not in seed and "WHEN NOT MATCHED THEN INSERT" in seed
    assert ("('OPS_PIPELINE_DEGRADED', 'PLATFORM', 'OVERWATCH pipeline self-watch: a telemetry source past its "
            "load cadence, a swallowed loader failure, or an idle alert notifier', TRUE, 'HIGH', 0, 24)") in seed
    assert ("('COST_IDLE_OPPORTUNITY', 'COST', 'Weekly idle-waste opportunity: a settings-verified AUTO_SUSPEND "
            "tightening recovers at least the threshold in USD per month', TRUE, 'MEDIUM', 100, 336)") in seed
    assert "AUTO_CLEAR" not in seed            # the two new rules keep the default (off)
    for name in re.findall(r"\('\w+', '\w+', '([^']*)'", seed):
        assert len(name) <= 200                # ALERT_CONFIG.NAME VARCHAR(200)


def test_v157_opt_in_update_after_both_procs_and_never_off():
    opt_in = _MIG.index("SET AUTO_CLEAR_ENABLED = TRUE")
    assert opt_in > _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN()")
    assert opt_in > _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN_DAILY()")
    assert _MIG.count("SET AUTO_CLEAR_ENABLED = TRUE") == 1
    assert " WHERE RULE_ID IN ('SEC_CRED_EXPIRY', 'SEC_NEW_EXPOSURE');" in _MIG
    assert "AUTO_CLEAR_ENABLED = FALSE" not in _MIG and "SET ENABLED" not in _MIG
    # the Guard-C config replay reads this as a flag-only UPDATE, never as enable/disable
    from tests.test_alert_rule_consistency import _ENABLED_SET_RE
    assert not _ENABLED_SET_RE.findall(_between(_MIG, "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG", ";"))


def test_v157_description_fits_and_doubles_apostrophes():
    desc = re.search(r"SELECT 157 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "'" not in desc.replace("''", "")
    assert len(desc.replace("''", "'")) <= 4000
    assert "SEC_BREAK_GLASS_USE" not in desc


def test_v157_markers_name_the_current_definer():
    from tests.test_proc_lineage import _lineage, _migrations
    rows = {(r["v"], r["proc"]): r for r in _lineage(_migrations())}
    for proc in ("SP_ALERT_SCAN", "SP_ALERT_SCAN_DAILY"):
        r = rows[(157, proc)]
        assert r["src"] == "marker" and r["claims"] == [141] and r["prev"] == 141, r


# -- normalize-and-compare: nothing else in V141 moved ------------------------------------------------

def test_v157_hourly_normalizes_back_to_v141_byte_for_byte():
    h = _H
    i, j = h.index("    -- [22] OPS_PIPELINE_DEGRADED"), h.index("    IF (fails > 0) THEN\n")
    h = h[:i] + h[j:]                                                            # [22] + [23]
    i, j = h.index("\n    -- [condition-ended sweep]"), h.index("\n    -- [snooze carry-forward sweep] V117:")
    h = h[:i] + h[j:]                                                            # condition-ended sweep
    assert h.count(_C1_LINE) == 1
    h = h.replace(_C1_LINE, "", 1)                                               # V091 scope line
    assert h.count(_H2_NEW) == 1
    h = h.replace(_H2_NEW, _H2_V141, 1)                                          # arm [10] recurrence fix
    i = h.index("    -- [hb] scan heartbeat")
    j = h.index("\n", h.index("    RETURN ", i)) + 1
    h = h[:i] + _RET_H141 + h[j:]                                                # [hb] + RETURN
    h = h.replace("    -- [17] COST_DEPT_BUDGET_PACE\n", _ARM15_V141 + "    -- [17] COST_DEPT_BUDGET_PACE\n", 1)
    assert h == _H141


def test_v157_daily_normalizes_back_to_v141_byte_for_byte():
    d = _D
    i, j = d.index("    -- [22] OPS_PIPELINE_DEGRADED"), d.index("    -- [17] PIPE_REF_GAP")
    d = d[:i] + d[j:]                                                            # [22] + [24]
    assert d.count("' of 11 daily alert rule block(s) failed this run'") == 1
    d = d.replace("' of 11 daily alert rule block(s) failed this run'",
                  "' of 9 daily alert rule block(s) failed this run'")
    i = d.index("    -- [hb] scan heartbeat")
    j = d.index("\n", d.index("    RETURN ", i)) + 1
    d = d[:i] + _RET_D141 + d[j:]
    assert d == _D141


def test_v157_keeps_the_intervening_carry_overs():
    # the V141 move of the 3 daily-grain cost arms off the hourly scan is not reverted
    for rule in ("COST_STORAGE_SURGE", "COST_SERVERLESS_CREEP", "COST_EGRESS_SPIKE"):
        assert rule not in _H and rule in _D, rule
    assert "-- Self-alert when any block failed" in _H141 and "-- Self-alert when any block failed" in _H
    assert "AS DAILY_BURN" in _D and "/ NULLIF(COUNT(DISTINCT DAY), 0)" in _D       # V064 burn (test_rec10)
    assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')" in _H                    # V096 supersede token
    assert "LEFT(s.DEDUPE_KEY, LENGTH(s.DEDUPE_KEY) - 11)" in _H                      # V117 carry-forward


# -- structural locks ---------------------------------------------------------------------------------

def test_v157_arm22_is_byte_identical_in_both_scans():
    assert _ARM22_H == _ARM22_D
    assert _H.count("    -- [22] OPS_PIPELINE_DEGRADED") == 1 and _D.count("    -- [22] OPS_PIPELINE_DEGRADED") == 1


def test_v157_dead_arm_and_its_query_history_read_are_gone():
    assert "SEC_BREAK_GLASS_USE" not in _H and "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY" not in _H
    assert "    -- [15]" not in _H
    assert _ARM15_V141.count(_FAILS_INC) == 1 and "ACCOUNT_USAGE.QUERY_HISTORY" in _ARM15_V141


def test_v157_tallies_equal_the_counting_arms():
    assert _H.count(_FAILS_INC) == 13 and _D.count(_FAILS_INC) == 11
    assert "' of 13 alert rule block(s) failed this run'" in _H
    assert "(13 - :fails) || '/13 rule blocks ok'" in _H
    assert "' of 11 daily alert rule block(s) failed this run'" in _D
    assert "(11 - :fails) || '/11 rule blocks ok (daily)'" in _D
    assert _ARM22_H.count(_FAILS_INC) == 1 and _ARM24.count(_FAILS_INC) == 1
    for block in (_ARM23, _CE, _HB_H, _HB_D):
        assert _FAILS_INC not in block and "fails :=" not in block


def test_v157_hourly_ordering():
    idx = [_H.index("CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE()"),
           _H.index("    IF (fails > 0) THEN"),
           _H.index("RESOLUTION_KIND = 'AUTO_CLEARED'"),
           _H.index("RESOLUTION_KIND = 'CONDITION_ENDED'"),
           _H.index("    -- [snooze carry-forward sweep]"),
           _H.index("    -- [hb] scan heartbeat"),
           _H.index("    RETURN 'alert scan v12 (V157:")]
    assert idx == sorted(idx), idx
    assert _H.index("    -- [22] OPS_PIPELINE_DEGRADED") < _H.index("    -- [23] PIPE_ETL_CYCLE")
    # [hb] is the LAST block, after the self-alert and every sweep
    assert _H.index("    END IF;\n") < _H.index("    -- [hb] scan heartbeat")


def test_v157_daily_ordering():
    idx = [_D.index("    -- [19] COST_EGRESS_SPIKE"), _D.index("    -- [22] OPS_PIPELINE_DEGRADED"),
           _D.index("    -- [24] COST_IDLE_OPPORTUNITY"), _D.index("    -- [17] PIPE_REF_GAP"),
           _D.index("    IF (fails > 0) THEN"), _D.index("    -- [hb] scan heartbeat"),
           _D.index("    RETURN 'alert scan daily v3 (V157:")]
    assert idx == sorted(idx), idx


def test_v157_machine_close_kinds():
    assert _H.count("RESOLUTION_KIND = 'CONDITION_ENDED'") == 2
    assert _H.count("RESOLUTION_KIND = 'AUTO_CLEARED'") == 1
    assert _H.count("RESOLUTION_KIND = 'SUPERSEDED'") == 1
    assert _H.count("RESOLUTION_KIND = 'SNOOZE_SUPPRESSED'") == 1
    assert "CONDITION_ENDED" not in _D


def test_v157_v091_sweep_is_scoped_to_its_three_perf_rules():
    assert _H.count(_C1_LINE) == 1 and _C1_LINE in _V091
    # the scope line lists exactly the rules whose still-firing set the NOT IN recomputes
    recomputed = set(re.findall(r"c\.RULE_ID = '(PERF_\w+)'", _V091))
    assert recomputed == {"PERF_QUERY_FAIL_PCT", "PERF_QUEUED_MINUTES", "PERF_SPILL_GB"}
    assert set(re.findall(r"'(PERF_\w+)'", _C1_LINE)) == recomputed


def test_v157_arm10_recurrence_fix_lives_only_in_arm10():
    for frag in ("COALESCE(e.RESOLUTION_KIND, '') NOT IN ('CONDITION_ENDED', 'SUPERSEDED')",
                 "REPLACE(b.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')",
                 "WHERE b.DEDUPE_KEY LIKE '%|EXPIRING'"):
        assert _H.count(frag) == 1 and frag in _ARM10, frag
    assert _H2_NEW in _ARM10
    # correlated subqueries only at top-level AND (error 002031 class): both sit in the outer WHERE
    tail = _ARM10[_ARM10.index(") b (RULE_ID, COMPANY"):]
    assert tail.index("WHERE NOT EXISTS (") < tail.index("AND NOT EXISTS (")
    assert " OR NOT EXISTS" not in tail and " OR EXISTS" not in tail


# -- [22] OPS_PIPELINE_DEGRADED -------------------------------------------------------------------------

def test_v157_arm22_cadence_fragment_is_the_app_health_strip_rule():
    from app.config import THRESHOLDS
    from app.data import mart_sql
    frag = (f"IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', "
            f"{THRESHOLDS['stale_daily_fact_hours']}, {THRESHOLDS['stale_fact_hours']})")
    assert frag == "IFF(SOURCE_NAME LIKE '%DAILY%' OR SOURCE_NAME LIKE '%METERING%', 30.0, 3.0)"
    assert frag in _ARM22_H and frag in mart_sql.health_strip()
    assert "FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE" in _ARM22_H


def test_v157_arm22_error_types_equal_the_native_stale_facts_alert():
    native = _between(_read("snowflake/native_alert_templates.sql"),
                      "CREATE OR REPLACE ALERT DBA_MAINT_DB.OVERWATCH.NATIVE_ALERT_STALE_FACTS", "THEN\n")
    native_types = set(re.findall(r"'(\w+_failed)'", _between(native, "WHERE ERROR_TYPE IN (", ")")))
    arm_types = set(re.findall(r"'(\w+_failed)'", _between(_ARM22_H, "WHERE ERROR_TYPE IN (", ")")))
    assert arm_types == native_types and len(arm_types) == 5


def test_v157_arm22_err_leg_skips_exactly_the_optional_mart_arms():
    """O-11: a persistently failing OPTIONAL SP_LOAD_MARTS_V27 arm is reported once per episode by the STALE
    leg, not once a day by the ERR leg -- and no REQUIRED arm's failure is ever skipped."""
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    marts = _latest_proc_bodies()["SP_LOAD_MARTS_V27"]
    arm_re = (r"'mart_load_failed', :emsg, '([A-Z0-9_]+)[^']*', CURRENT_ROLE\(\);\n\s*"
              r"(opt_fail|req_fail) := \2 \+ 1;")
    optional = {src for src, kind in re.findall(arm_re, marts) if kind == "opt_fail"}
    required = {src for src, kind in re.findall(arm_re, marts) if kind == "req_fail"}
    assert optional and required and not (optional & required)
    skipped = set(re.findall(r"'(\w+)'", _between(_ARM22_H, "NOT IN ('MART_TAG_COVERAGE", ")")
                             .replace("NOT IN (", "NOT IN ('", 0)))
    skipped = set(re.findall(r"'([A-Z0-9_]+)'", _between(_ARM22_H, "' ', 1) NOT IN (", ")\n")))
    assert skipped == optional == {"MART_TAG_COVERAGE_DAILY", "MART_TASK_NODE_DAILY", "FACT_AI_USAGE_DAILY"}


def test_v157_arm22_keys_clocks_and_bounds():
    a = _ARM22_H
    assert ("c.RULE_ID || '|STALE|' || f.SOURCE_NAME || '|' || "
            "COALESCE(TO_VARCHAR(TO_DATE(f.LAST_LOAD_TS)), 'NEVER')") in a
    assert "c.RULE_ID || '|ERR|' || x.ERROR_TYPE || '|' || x.SRC || '|' || TO_VARCHAR(x.ERR_DAY)" in a
    assert "c.RULE_ID || '|NOTIFY|' || COALESCE(TO_VARCHAR(TO_DATE(l.ACQUIRED_AT)), 'NEVER')" in a
    assert "|STALE|ALERT_NOTIFY|" not in a        # a future ALERT_NOTIFY freshness row can never collide
    # every clock pinned to Central (TIMEZONE STANDARD): the NTZ stamps read here are Central wall-clock
    assert a.count("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ") == 3
    assert not re.search(r"DATEDIFF\('minute', \w+, CURRENT_TIMESTAMP\(\)\)", a)
    assert "DATEADD('hour', -24, CURRENT_TIMESTAMP())" not in a
    # free text bounded to ALERT_EVENTS (TITLE 300, DETAIL 2000); the lease leg needs an enabled route
    assert a.count(", 300),") == 3 and a.count(", 2000),") == 3
    assert "OW_SENDER_LEASE" in a and "ALERT_ROUTES r WHERE r.ENABLED" in a and "l.AGE_MIN > 180" in a
    assert "COALESCE(f.STATUS, '—')" in a and "COALESCE(LEFT(x.LAST_MSG, 600), '—')" in a
    assert "every run that acquires the lease" in a
    assert "the scan stopped, or its heartbeat stamp failed (APP_ERROR_LOG scan_heartbeat_failed)" in a
    assert "the other graph raised this" not in a
    assert "SNOWFLAKE.ACCOUNT_USAGE" not in a      # OVERWATCH-internal tables only


# -- [23] ETL-cycle add-on ---------------------------------------------------------------------------------

def test_v157_arm23_is_a_non_counting_add_on():
    a = _ARM23
    assert "        CALL DBA_MAINT_DB.OVERWATCH.SP_SCAN_ETL_CYCLE();\n" in a
    assert "'etl_cycle_scan_failed', LEFT(:emsg, 2000)" in a
    assert "NOT counted toward the" in a and _FAILS_INC not in a
    assert a.count("BEGIN") == 1 and a.count("END;") == 1


# -- [hb] heartbeats -----------------------------------------------------------------------------------

@pytest.mark.parametrize(("hb", "source", "total", "status"), [
    (_HB_H, "ALERT_SCAN_HOURLY", 13, "'alert scan ' || (13 - :fails) || '/13 rule blocks ok' AS STATUS"),
    (_HB_D, "ALERT_SCAN_DAILY", 11, "'alert scan daily ' || (11 - :fails) || '/11 rule blocks ok (daily)' AS STATUS"),
])
def test_v157_heartbeats(hb, source, total, status):
    assert f"SELECT '{source}' AS SOURCE_NAME," in hb
    assert "CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ AS LAST_LOAD_TS" in hb
    assert f"({total} - :fails) AS ROW_COUNT" in hb and status in hb
    assert "GENERATION = COALESCE(t.GENERATION, 0) + 1" in hb
    assert f"'{source} heartbeat stamp - alerts unaffected'" in hb      # CONTEXT starts with the source name
    assert "'scan_heartbeat_failed'" in hb and hb.endswith("    END;\n\n")
    # the shared name rule the OTHER graph's [22] applies: hourly 3h, daily 30h
    lim = 30.0 if ("DAILY" in source or "METERING" in source) else 3.0
    assert lim == (30.0 if source == "ALERT_SCAN_DAILY" else 3.0)


# -- #12c condition-ended sweep -----------------------------------------------------------------------------

def test_v157_condition_ended_block_shape():
    ce = _CE
    assert ce.startswith("    -- [condition-ended sweep] V157 (Next-Fifty #12c): ")
    assert ce.endswith("    END;\n") and _H.count("    -- [condition-ended sweep]") == 1
    for rule in ("SEC_CRED_EXPIRY", "SEC_NEW_EXPOSURE"):          # one EXISTS-with-join gate per rule
        gate = ("        IF (EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n"
                "                    JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID\n"
                f"                    WHERE e.RULE_ID = '{rule}' AND e.STATUS = 'OPEN'\n"
                "                      AND c.ENABLED AND c.AUTO_CLEAR_ENABLED)) THEN\n")
        assert ce.count(gate) == 1, rule
        assert f"'V157 condition-ended sweep {rule} - other rules unaffected'" in ce
    assert ce.count("WHERE ev.STATUS = 'OPEN'") == 2                         # OPEN only
    assert ce.count("ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())") == 2   # 1h dwell
    assert "WHERE ENABLED AND AUTO_CLEAR_ENABLED AND THRESHOLD_NUM IS NOT NULL" in ce
    assert "EXISTS (SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS)" in ce        # empty read never clears
    assert "GREATEST(c.THRESHOLD_NUM, COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM))" in ce
    assert "HAVING COUNT_IF(g.DELETED_ON IS NULL) = 0" in ce
    assert "AND g.CREATED_ON >= DATEADD('day', -400, CURRENT_TIMESTAMP())" in ce
    # no nested scalar subquery (Snowflake 002031) and never an ACK/SNOOZED row
    assert "(SELECT MIN(" not in ce and "(SELECT MAX(" not in ce
    assert "'ACK'" not in ce and "'SNOOZED'" not in ce
    assert ce.count("condition_ended_sweep_failed") == 2


def test_v157_condition_ended_keys_are_rebuilt_with_the_raise_arms_expressions():
    # SEC_CRED_EXPIRY: arm [10]'s key prefix + its two band literals
    prefix = "c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|'"
    assert prefix in _ARM10 and (prefix + " || bd.BAND AS DEDUPE_KEY") in _CE
    arm10_bands = set(re.findall(
        r"IFF\(cr\.EXPIRATION_DATE < CURRENT_TIMESTAMP\(\), '(\w+)', '(\w+)'\)\n", _ARM10)[0])
    ce_bands = set(re.findall(r"SELECT '(\w+)' AS BAND UNION ALL SELECT '(\w+)'", _CE)[0])
    assert arm10_bands == ce_bands == {"EXPIRED", "EXPIRING"}
    # the clear window is never narrower than arm [10]'s raise window
    assert "cr.EXPIRATION_DATE <= DATEADD('day', c.THRESHOLD_NUM, CURRENT_TIMESTAMP())" in _ARM10
    # SEC_NEW_EXPOSURE: arm [20]'s key, alias p -> g
    tail20 = " || '|' || p.PRIVILEGE || '|' || p.GRANTED_ON || '|' || TO_VARCHAR(p.CREATED_ON)"
    assert ("c.RULE_ID" + tail20) in _ARM20 and "c.RULE_ID = 'SEC_NEW_EXPOSURE'" in _ARM20
    assert ("'SEC_NEW_EXPOSURE'" + tail20.replace("p.", "g.")) in _CE
    assert "GROUP BY g.PRIVILEGE, g.GRANTED_ON, g.CREATED_ON" in _CE
    assert "GROUP BY PRIVILEGE, GRANTED_ON, CREATED_ON" in _ARM20


# -- [24] COST_IDLE_OPPORTUNITY ----------------------------------------------------------------------------

def test_v157_arm24_constants_match_the_app():
    from app.data import mart27_sql
    from app.logic import insights, remediation
    a = _ARM24
    assert f"s.IDLE_PCT >= {insights.IDLE_PCT_FLAG:g}" in a
    assert f"s.IDLE_CREDITS >= {insights.IDLE_MIN_CREDITS:g}" in a
    assert f"* ({insights.IDLE_RESUME_TAIL_SEC} / 3600.0)" in a
    t = insights.IDLE_TARGET_SUSPEND_SEC
    assert (f"GREATEST(30, LEAST(IFF(w.AUTO_SUSPEND > 0, LEAST(w.AUTO_SUSPEND, {t}), {t}), 3600)) AS TARGET_SEC"
            in a)
    assert f"(w.AUTO_SUSPEND <= 0 OR w.AUTO_SUSPEND > {t})" in a
    # the clamp mirrors remediation.auto_suspend_fix (30..3600) and the target its tighten plan
    assert remediation.auto_suspend_fix("WH_X", 5).endswith("= 30;")
    assert remediation.auto_suspend_fix("WH_X", 99999).endswith("= 3600;")
    assert remediation.tighten_suspend_plan("WH_X", 600, True)["stmt"].endswith(f"= {t};")
    assert f"REGEXP_LIKE(o.WAREHOUSE_NAME, '{remediation._IDENT_RE.pattern}')" in a
    live = mart27_sql.eff_idle_analysis(14)
    for frag in ("COALESCE(IDLE_CREDITS, CREDITS_TOTAL * COALESCE(IDLE_PCT, 0) / 100)",
                 "GREATEST(SUM(BILLED_HOURS) - SUM(ACTIVE_HOURS), 0) AS IDLE_HOURS",
                 "UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'", "HAVING SUM(CREDITS_TOTAL) > 0"):
        assert frag in a and frag in live, frag


def test_v157_arm24_window_clock_key_and_text():
    a = _ARM24
    assert "SELECT CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE AS TODAY" in a
    assert "WHERE DAY >= DATEADD('day', -14, clk.TODAY) AND DAY < clk.TODAY" in a   # 14 COMPLETE days
    assert "WHERE s.COVERED_DAYS >= 7" in a
    assert "TO_VARCHAR(DATEADD('day', 1 - DAYOFWEEKISO(k.TODAY), k.TODAY))" in a    # ISO Monday, Central
    assert "IFF(o.MONTHLY_USD >= c.THRESHOLD_NUM * 5, 'HIGH', 'MED')" in a            # band token -> V067 sweep
    assert "IFF(o.MONTHLY_USD >= c.THRESHOLD_NUM * 5, 'HIGH', c.SEVERITY)" in a
    assert "COALESCE(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(o.WAREHOUSE_NAME), 'ALL')" in a
    assert "SNAPSHOT_AT >= DATEADD('hour', -36, CURRENT_TIMESTAMP())" in a
    assert "SNOWFLAKE.ACCOUNT_USAGE" not in a
    assert "about the ACTIONABLE figure Optimize shows with a 14-day window; this alert uses 14 " in a
    assert "(an in-app $0 twin, if any, is superseded)" in a
    assert "(no manual ledger row)" not in a and "Idle & sizing ACTIONABLE figure)" not in a
    assert a.count(", 300),") == 1 and "\n                   2000),\n" in a
    assert "$$" not in a


def _DAYS(n: int) -> timedelta:
    return timedelta(days=n)


def _half_up(x: float, nd: int) -> float:
    return float(Decimal(str(float(x))).quantize(Decimal(1).scaleb(-nd), rounding=ROUND_HALF_UP))


def _arm24_oracle(daily: pd.DataFrame, snap: pd.DataFrame, today: date, price: float,
                  threshold: float) -> pd.DataFrame:
    """pandas twin of [24]'s win -> cov -> idle -> scored -> cur -> opp chain (one arm row per warehouse)."""
    win = daily[(daily["DAY"] >= today - _DAYS(14)) & (daily["DAY"] < today)
                & (daily["WAREHOUSE_NAME"].str.upper() != "CLOUD_SERVICES_ONLY")].copy()
    idle_pct = pd.to_numeric(win["IDLE_PCT"], errors="coerce").fillna(0.0)
    win["IDLE_CR"] = win["IDLE_CREDITS"].where(win["IDLE_CREDITS"].notna(), win["CREDITS_TOTAL"] * idle_pct / 100)
    covered = win["DAY"].nunique()
    rows = []
    for wh, g in win.groupby("WAREHOUSE_NAME"):
        metered, total, idle = g["BILLED_HOURS"].sum(), g["CREDITS_TOTAL"].sum(), g["IDLE_CR"].sum()
        if total <= 0:
            continue
        idle_hours = max(metered - g["ACTIVE_HOURS"].sum(), 0)
        idle_pct = _half_up(idle / total * 100, 1)
        per_hour = total / metered if metered else 0
        recoverable = max(idle - max(metered - idle_hours, 0) * (60 / 3600.0) * per_hour, 0)
        cur = snap[snap["WAREHOUSE_NAME"].str.upper() == wh.upper()]
        if cur.empty:
            continue
        suspend = cur.iloc[0]["AUTO_SUSPEND"]
        if not (covered >= 7 and idle_pct >= 20 and idle >= 1 and pd.notna(suspend)
                and (suspend <= 0 or suspend > 60)):
            continue
        monthly = _half_up(recoverable * price / covered * 30, 2)
        if monthly >= threshold:
            rows.append({"WAREHOUSE_NAME": wh, "MONTHLY_USD": monthly})
    return pd.DataFrame(rows, columns=["WAREHOUSE_NAME", "MONTHLY_USD"])


def test_v157_arm24_pandas_oracle_matches_insights_idle_advisor():
    """Formula parity with the Optimize ACTIONABLE figure: same flag, same resume-tail haircut, same
    settings gate, same USD/month -- on the aggregated frame eff_idle_analysis serves the page."""
    from app.logic.insights import idle_advisor
    today = date(2026, 9, 28)                          # a Monday; the window is 09-14 .. 09-27
    days = [today - _DAYS(k) for k in range(1, 15)]
    spec = {   # WAREHOUSE: (credits/day, idle credits/day, billed h/day, active h/day, AUTO_SUSPEND)
        "WH_A": (10.0, 4.5, 10.0, 6.0, 600),           # 45% idle, 10-minute timer -> actionable
        "WH_B": (10.0, 4.5, 10.0, 6.0, 30),            # already tighter than 60s -> excluded by both
        "WH_C": (10.0, 4.5, 10.0, 6.0, 0),             # never suspends -> actionable (enable a timer)
        "WH_D": (10.0, 4.5, 10.0, 6.0, None),          # unknown timer -> excluded by both
        "WH_E": (10.0, 1.996, 10.0, 8.0, 300),         # 19.96% idle rounds to 20.0 -> FLAGGED by both
        "WH_F": (0.5, 0.06, 2.0, 1.0, 600),            # < 1 idle credit in the window -> not flagged
    }
    daily = pd.DataFrame([{"WAREHOUSE_NAME": wh, "DAY": d, "CREDITS_TOTAL": c, "IDLE_CREDITS": i,
                           "IDLE_PCT": None, "BILLED_HOURS": b, "ACTIVE_HOURS": a}
                          for wh, (c, i, b, a, _s) in spec.items() for d in days])
    # rows outside the window (today's partial day, day -15) never count
    daily = pd.concat([daily, pd.DataFrame([
        {"WAREHOUSE_NAME": "WH_A", "DAY": today, "CREDITS_TOTAL": 99.0, "IDLE_CREDITS": 99.0,
         "IDLE_PCT": None, "BILLED_HOURS": 1.0, "ACTIVE_HOURS": 0.0},
        {"WAREHOUSE_NAME": "WH_A", "DAY": today - _DAYS(15), "CREDITS_TOTAL": 99.0,
         "IDLE_CREDITS": 99.0, "IDLE_PCT": None, "BILLED_HOURS": 1.0, "ACTIVE_HOURS": 0.0}])], ignore_index=True)
    snap = pd.DataFrame([{"WAREHOUSE_NAME": wh, "AUTO_SUSPEND": s} for wh, (*_x, s) in spec.items()])
    price = 3.68

    arm = _arm24_oracle(daily, snap, today, price, threshold=0.0).set_index("WAREHOUSE_NAME")

    # the app side: eff_idle_analysis' aggregate over the same 14 complete days, then idle_advisor
    win = daily[(daily["DAY"] >= today - _DAYS(14)) & (daily["DAY"] < today)]
    agg = win.groupby("WAREHOUSE_NAME").agg(METERED_HOURS=("BILLED_HOURS", "sum"),
                                            ACTIVE=("ACTIVE_HOURS", "sum"),
                                            TOTAL_CREDITS=("CREDITS_TOTAL", "sum"),
                                            IDLE_CREDITS=("IDLE_CREDITS", "sum")).reset_index()
    agg["IDLE_HOURS"] = (agg["METERED_HOURS"] - agg["ACTIVE"]).clip(lower=0)
    agg = agg.drop(columns="ACTIVE").merge(snap, on="WAREHOUSE_NAME")
    agg["AUTO_SUSPEND_KNOWN"] = agg["AUTO_SUSPEND"].notna()
    app = idle_advisor(agg, price, win["DAY"].nunique()).set_index("WAREHOUSE_NAME")

    app_actionable = set(app.index[app["ACTIONABLE"].astype(bool)])
    assert set(arm.index) == app_actionable == {"WH_A", "WH_C", "WH_E"}
    for wh in app_actionable:
        assert abs(arm.loc[wh, "MONTHLY_USD"] - app.loc[wh, "ACTIONABLE_MONTHLY_USD"]) <= 0.05, wh
    assert app.loc["WH_E", "IDLE_PCT"] == 20.0 and bool(app.loc["WH_E", "FLAGGED"])
    assert not bool(app.loc["WH_F", "FLAGGED"]) and not bool(app.loc["WH_B", "ACTIONABLE"])
    assert app.loc["WH_B", "ACTION_STATUS"] == "ALREADY TUNED"
    assert app.loc["WH_D", "ACTION_STATUS"] == "VERIFY SETTING"
    # the threshold gate and the COVERED_DAYS >= 7 gate
    top = arm["MONTHLY_USD"].max()
    assert _arm24_oracle(daily, snap, today, price, threshold=top + 0.01).empty
    young = daily[daily["DAY"] >= today - _DAYS(6)]
    assert _arm24_oracle(young, snap, today, price, threshold=0.0).empty


# -- app lockstep owned by this slice -----------------------------------------------------------------------

def test_v157_playbooks_navigation_and_evidence():
    from app.logic import navigate
    from app.logic.alert_evidence import plan_for_alert
    from app.logic.playbooks import PLAYBOOKS
    pb = PLAYBOOKS["OPS_PIPELINE_DEGRADED"]
    assert "ALERT_SCAN_HOURLY" in pb and "Diagnose stale sources" in pb and "loader_chain_check.sql" in pb
    assert "scan_heartbeat_failed" in pb and "at most once per last-load day" in pb
    pb = PLAYBOOKS["COST_IDLE_OPPORTUNITY"]
    assert "Idle & sizing" in pb and "about the same actionable USD/month" in pb
    assert "never-suspend warehouse is not auto-booked" in pb
    assert "CONDITION_ENDED" in PLAYBOOKS["SEC_CRED_EXPIRY"] and "MITIGATED" in PLAYBOOKS["SEC_CRED_EXPIRY"]
    assert "CONDITION_ENDED" in PLAYBOOKS["SEC_NEW_EXPOSURE"]
    assert "weekly (Mondays 05:30 Central)" in PLAYBOOKS["OPS_CANARY_FAIL"]
    assert "hourly source canary" not in PLAYBOOKS["OPS_CANARY_FAIL"]
    assert "CRON 30 5 * * 1 America/Chicago" in (_MIGDIR / "V016__closing_loops.sql").read_text(encoding="utf-8")
    assert navigate._RULE_TARGETS["OPS_PIPELINE_DEGRADED"] == ("Control Room", "Freshness & replay")
    assert navigate._RULE_TARGETS["COST_IDLE_OPPORTUNITY"] == ("Cost Intelligence", "Optimization & Savings")
    assert navigate.FIX_TARGETS["COST_IDLE_OPPORTUNITY"] == ("Cost Intelligence", "Optimization & Savings")
    title = "WH_ALFA_BI_PRD idle waste ~$412/mo: AUTO_SUSPEND 600s -> 60s"
    assert navigate.inline_fix_warehouse("COST_IDLE_OPPORTUNITY", title) == "WH_ALFA_BI_PRD"
    assert plan_for_alert("COST_IDLE_OPPORTUNITY", title, "", "2026-09-28") is None


def test_v157_runbook_catalogue():
    rb = _read("RUNBOOK.md")
    assert "| OPS_PIPELINE_DEGRADED | PLATFORM |" in rb and "| COST_IDLE_OPPORTUNITY | COST |" in rb
    assert "~~SEC_BREAK_GLASS_USE~~" in rb                                   # history row stays
    assert "| SEC_BREAK_GLASS_USE | SECURITY | > threshold statements/day" not in rb   # live-looking row gone


def test_v157_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")


def test_v157_inserted_select_statements_parse():
    """The new arms' INSERT ... WITH statements parse as Snowflake SQL once the :binds are literals."""
    sqlglot = pytest.importorskip("sqlglot")
    for block in (_ARM22_H, _ARM24):
        stmt = block[block.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):block.index("    EXCEPTION")]
        sqlglot.parse(stmt.replace(":credit_price", "3.68"), dialect="snowflake")
    for gate in re.findall(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS ev.*?;\n", _CE, re.S):
        sqlglot.parse(gate, dialect="snowflake")


# -- integration lockstep (validate floor / docs / Admin). Pinned to the WAVE TIP V158; these are completed
#    by the wave-2b integrator (snowflake/validate.sql, DEPLOYMENT.md, README.md and
#    admin._EXPECTED_MIGRATIONS are shared files a single slice does not edit).

def test_validate_and_docs_track_v157():
    val = _read("snowflake/validate.sql")
    assert "V001..V158 applied" in val and "VERSION BETWEEN 1 AND 158) = 158" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert _NAME in _read(rel), rel


def test_v157_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 157 in _EXPECTED_MIGRATIONS
