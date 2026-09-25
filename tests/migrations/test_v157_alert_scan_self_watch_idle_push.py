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

import math
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
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
# arm [10] recurrence fix (#12c + review fix): the key is unchanged; EXP_TS / WIN_DAYS ride out of b so a closed
# row blocks only inside THIS expiry's warning window. Test-side copies, independent of the generator.
_H2A_V141 = ("               c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || "
             "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING')\n        FROM cfg c\n")
_H2A_NEW = ("               c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || "
            "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING'),\n"
            "               cr.EXPIRATION_DATE,   -- V157: EXP_TS (this cycle's expiry; dedupe only, not inserted)\n"
            "               c.THRESHOLD_NUM       -- V157: WIN_DAYS (the raise window)\n"
            "        FROM cfg c\n")
_H2B_V141 = "        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)\n        WHERE NOT EXISTS (\n"
_H2B_NEW = ("        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY, EXP_TS, WIN_DAYS)\n"
            "        WHERE NOT EXISTS (\n")
_H2_WINDOW = ("              AND COALESCE(e.RESOLVED_AT, CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())"
              "::TIMESTAMP_NTZ)\n"
              "                  >= DATEADD('day', -b.WIN_DAYS, CONVERT_TIMEZONE('America/Chicago', b.EXP_TS)"
              "::TIMESTAMP_NTZ)\n")
_H2_V141 = "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n        );\n"
_H2_NEW = (
    "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
    "              AND COALESCE(e.RESOLUTION_KIND, '') NOT IN ('CONDITION_ENDED', 'SUPERSEDED')   "
    "-- V157: a machine close never blocks\n"
    "              -- V157: the key has no date, so a row closed (by anyone: ACTIONED, NOISE, EXPECTED, a bulk "
    "clear) before THIS\n"
    "              -- expiry's warning window opened is an earlier cycle and never blocks -- a rotated "
    "credential's next expiry\n"
    "              -- re-alerts. A live row (RESOLVED_AT NULL) or a close inside this window still blocks. "
    "Central wall-clock.\n"
    + _H2_WINDOW +
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
    assert h.count(_H2_NEW) == 1 and h.count(_H2A_NEW) == 1 and h.count(_H2B_NEW) == 1
    h = h.replace(_H2_NEW, _H2_V141, 1)                                          # arm [10] recurrence fix
    h = h.replace(_H2A_NEW, _H2A_V141, 1).replace(_H2B_NEW, _H2B_V141, 1)          # + carried EXP_TS/WIN_DAYS
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
                 "WHERE b.DEDUPE_KEY LIKE '%|EXPIRING'",
                 _H2_WINDOW, "DEDUPE_KEY, EXP_TS, WIN_DAYS)", "b.WIN_DAYS", "b.EXP_TS"):
        assert _H.count(frag) == 1 and frag in _ARM10, frag
    assert _H2_NEW in _ARM10 and _H2A_NEW in _ARM10 and _H2B_NEW in _ARM10
    # EXP_TS / WIN_DAYS feed the dedupe only: the INSERT still writes exactly the 7 event columns
    assert ("        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY\n"
            "        FROM (\n") in _ARM10
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


# -- sqlite execution harness ---------------------------------------------------------------------------------
# The behavioural tests below EXECUTE the shipped arm / sweep text (never a hand-written twin of it) in an
# in-memory sqlite, so a formula, gate, window or polarity edit made in outputs/gen_v157.py -- which the
# byte-identity test happily follows -- still fails here. Every clock is Central wall-clock as fractional days
# (date.toordinal() + fraction): the account TIMEZONE is Central, so CONVERT_TIMEZONE('America/Chicago',
# x)::TIMESTAMP_NTZ is the identity. A DAY column is an integer ordinal. The translation is minimal and fails
# closed: a Snowflake cast, QUALIFY or bind it does not know raises instead of being skipped.

_SCHEMAS = {
    "ALERT_CONFIG": ("RULE_ID", "SEVERITY", "ENABLED", "THRESHOLD_NUM", "CLEAR_THRESHOLD_NUM",
                     "AUTO_CLEAR_ENABLED"),
    "ALERT_EVENTS": ("EVENT_ID", "RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL", "METRIC_VALUE",
                     "DEDUPE_KEY", "STATUS", "RESOLUTION_KIND", "RAISED_AT", "RESOLVED_AT"),
    "CREDENTIALS": ("USER_NAME", "NAME", "TYPE", "EXPIRATION_DATE"),
    "GRANTS_TO_ROLES": ("GRANTEE_NAME", "PRIVILEGE", "GRANTED_ON", "NAME", "GRANTED_BY", "CREATED_ON",
                        "DELETED_ON"),
    "MART_WAREHOUSE_EFFICIENCY_DAILY": ("WAREHOUSE_NAME", "DAY", "BILLED_HOURS", "ACTIVE_HOURS",
                                        "CREDITS_TOTAL", "IDLE_CREDITS", "IDLE_PCT"),
    "WAREHOUSE_CONFIG_SNAPSHOT": ("WAREHOUSE_NAME", "AUTO_SUSPEND", "SNAPSHOT_AT"),
    "SETTINGS": ("KEY", "VALUE"),
}
_EVENT_COLS = ("RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL", "METRIC_VALUE", "DEDUPE_KEY")


def _ts(d: date, hour: float = 0.0) -> float:
    return d.toordinal() + hour / 24.0


def _dt(x: float) -> datetime:
    whole = math.floor(x)
    return datetime.fromordinal(int(whole)) + timedelta(seconds=round((x - whole) * 86400))


def _sf_dateadd(unit, n, x):
    if n is None or x is None:
        return None
    u = str(unit).lower()
    if u == "day" and isinstance(x, int) and float(n).is_integer():
        return x + int(n)                                    # a DATE stays a DATE
    return x + n / {"day": 1.0, "hour": 24.0, "minute": 1440.0}[u]


def _sf_datediff(unit, a, b):
    if a is None or b is None:
        return None
    per = {"day": 1, "hour": 24, "minute": 1440}[str(unit).lower()]
    return math.floor(b * per) - math.floor(a * per)


def _sf_to_varchar(x, fmt=None):
    if x is None or isinstance(x, str):
        return x
    if isinstance(x, int):
        return date.fromordinal(x).isoformat()
    return _dt(x).strftime({None: "%Y-%m-%d %H:%M:%S", "YYYY-MM-DD": "%Y-%m-%d",
                            "YYYY-MM-DD HH24:MI": "%Y-%m-%d %H:%M"}[fmt])


def _sf_try_to_double(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _nullsafe(fn):
    return lambda *a: None if any(v is None for v in a) else fn(*a)


class _CountIf:
    def __init__(self) -> None:
        self.n = 0

    def step(self, v) -> None:
        self.n += 1 if v else 0

    def finalize(self) -> int:
        return self.n


def _connect(tables: dict, now: float) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    funcs = {
        "NOW_TS": (0, lambda: now), "TODAY_D": (0, lambda: math.floor(now)),
        "DATEADD": (3, _sf_dateadd), "DATEDIFF": (3, _sf_datediff),
        "GREATEST": (-1, _nullsafe(lambda *a: max(a))), "LEAST": (-1, _nullsafe(lambda *a: min(a))),
        "IFF": (3, lambda c, a, b: a if c else b),
        "LEFT": (2, _nullsafe(lambda s, n: str(s)[:int(n)])),
        "TO_VARCHAR": (-1, _sf_to_varchar), "TRY_TO_DOUBLE": (1, _sf_try_to_double),
        "DAYOFWEEKISO": (1, _nullsafe(lambda d: date.fromordinal(int(d)).isoweekday())),
        "REGEXP_LIKE": (2, _nullsafe(lambda s, p: int(re.fullmatch(p, str(s)) is not None))),
        "COMPANY_FOR_WAREHOUSE": (1, lambda _w: None), "COMPANY_FOR_USER": (1, lambda _u: None),
    }
    for name, (narg, fn) in funcs.items():
        con.create_function(name, narg, fn, deterministic=True)
    con.create_aggregate("COUNT_IF", 1, _CountIf)
    for table, cols in _SCHEMAS.items():
        con.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
        for row in tables.get(table, ()):
            assert set(row) <= set(cols), (table, set(row) - set(cols))
            con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                        tuple(row.values()))
    return con


def _qualify_to_rn(sql: str) -> str:
    """CTE ``x AS (SELECT <cols> ... QUALIFY <rn> = 1)`` -> a ROW_NUMBER subquery (sqlite has no QUALIFY)."""
    while m := re.search(r"\n[ ]*QUALIFY (?P<rn>[^\n]+) = 1\n", sql):
        start = sql.rindex(" AS (\n", 0, m.start()) + len(" AS (\n")
        first, rest = sql[start:m.start()].split("\n", 1)
        assert first.lstrip().startswith("SELECT "), first
        sql = (sql[:start] + "SELECT * FROM (" + first + f", {m['rn']} AS QRN_\n" + rest
               + "\n) WHERE QRN_ = 1\n" + sql[m.end():])
    return sql


def _derived_to_cte(sql: str) -> str:
    """``SELECT b.* FROM (<inner>) b (cols) WHERE ...`` -> CTE ``b(cols)`` (no derived column lists in sqlite)."""
    m = re.search(r"\n(?P<sel>[ ]*SELECT b\.RULE_ID[^\n]*)\n[ ]*FROM \(\n(?P<inner>.*)\n[ ]*\) b "
                  r"\((?P<cols>[^)]*)\)\n", sql, re.S)
    if not m:
        return sql
    return (sql[:m.start()].rstrip() + f",\nb({m['cols']}) AS (\n{m['inner']}\n)\n{m['sel']}\nFROM b\n"
            + sql[m.end():])


def _to_sqlite(sql: str, price: float = 3.68) -> str:
    s = sql.replace("DBA_MAINT_DB.OVERWATCH.", "").replace("SNOWFLAKE.ACCOUNT_USAGE.", "")
    s = s.replace("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE", "TODAY_D()")
    s = s.replace("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ", "NOW_TS()")
    s = s.replace("CURRENT_TIMESTAMP()", "NOW_TS()")
    s = re.sub(r"CONVERT_TIMEZONE\('America/Chicago', ([\w.]+)\)::TIMESTAMP_NTZ", r"\1", s)
    s = re.sub(r"(ROUND\([^()]*\))::INT\b", r"CAST(\1 AS INTEGER)", s)
    s = s.replace(":credit_price", repr(float(price)))
    s = re.sub(r"\bUPDATE (\w+) (\w+)\n", r"UPDATE \1 AS \2\n", s)
    s = _derived_to_cte(_qualify_to_rn(s))
    assert "::" not in s and "QUALIFY" not in s and re.search(r":\w", s.replace("HH24:MI", "")) is None, s
    return s


def _arm_statement(block: str) -> str:
    """The arm's ``WITH ... SELECT`` (its INSERT column list dropped)."""
    return block[block.index("        WITH cfg AS ("):block.index(";\n    EXCEPTION")]


def _run_arm(block: str, tables: dict, now: float, price: float = 3.68) -> list[dict]:
    cur = _connect(tables, now).execute(_to_sqlite(_arm_statement(block), price))
    cols = tuple(c[0] for c in cur.description)
    assert cols == _EVENT_COLS, cols                  # the INSERT writes exactly the 7 event columns
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _ce_updates(ce: str) -> dict:
    ups = re.findall(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS ev.*?;\n", ce, re.S)
    return {re.search(r"AND ev\.RULE_ID = '(\w+)'", u).group(1): u for u in ups}


def _run_ce(tables: dict, now: float) -> dict:
    """Run BOTH condition-ended UPDATEs against the tables; -> {EVENT_ID: row} afterwards."""
    con = _connect(tables, now)
    updates = _ce_updates(_CE)
    assert set(updates) == {"SEC_CRED_EXPIRY", "SEC_NEW_EXPOSURE"}
    for stmt in updates.values():
        con.execute(_to_sqlite(stmt))
    cur = con.execute("SELECT * FROM ALERT_EVENTS")
    cols = [c[0] for c in cur.description]
    return {r[0]: dict(zip(cols, r, strict=True)) for r in cur.fetchall()}


def _raised(rows: list[dict], at: float, prefix: str) -> list[dict]:
    """Arm output rows -> OPEN ALERT_EVENTS rows raised at ``at`` (EVENT_ID = prefix + ordinal)."""
    return [dict(r, EVENT_ID=f"{prefix}{i}", STATUS="OPEN", RAISED_AT=at) for i, r in enumerate(rows)]


_NOW = _ts(date(2026, 9, 30), 10.0)                     # a Wednesday, 10:00 Central


def _cfg(rule: str, **over) -> dict:
    row = {"RULE_ID": rule, "SEVERITY": "HIGH", "ENABLED": 1, "THRESHOLD_NUM": 14, "CLEAR_THRESHOLD_NUM": None,
           "AUTO_CLEAR_ENABLED": 1}
    row.update(over)
    return row


def _cred(u: str, exp: float) -> dict:
    return {"USER_NAME": f"SVC_{u}", "NAME": f"TOK_{u}", "TYPE": "PROGRAMMATIC_ACCESS_TOKEN", "EXPIRATION_DATE": exp}


def _ckey(u: str, band: str) -> str:
    return f"SEC_CRED_EXPIRY|SVC_{u}|TOK_{u}|{band}"


def _ev(key: str, status: str, kind: str | None = None, raised: float | None = None,
        resolved: float | None = None) -> dict:
    return {"EVENT_ID": key, "RULE_ID": key.split("|", 1)[0], "DEDUPE_KEY": key, "STATUS": status,
            "RESOLUTION_KIND": kind, "RAISED_AT": raised, "RESOLVED_AT": resolved}


# -- #12c / review fix: arm [10] dedupe is cycle-aware (EXECUTED) ------------------------------------------------

def test_v157_arm10_prior_cycle_close_never_blocks_a_same_cycle_close_does():
    """The key has no date. A row closed (by ANYONE -- a human ACTIONED/NOISE/EXPECTED or a blank-kind bulk
    clear) before this expiry's warning window opened (EXP - THRESHOLD_NUM days) is an earlier cycle and must
    not block; a live row or a close inside the window still blocks; a machine close never blocks; and the
    h-leg still never mints EXPIRING while the EXPIRED event is live."""
    n = _NOW
    creds = [_cred(u, n + 5) for u in "ABDEFGIJ"] + [_cred("C", n - 1), _cred("H", n + 30)]
    events = [
        # prior cycle, rotated early and resolved ACTIONED by a human -> must NOT block (re-alerts)
        _ev(_ckey("A", "EXPIRING"), "RESOLVED", "ACTIONED", raised=n - 100, resolved=n - 95),
        # same cycle, resolved NOISE after the window opened (n - 9) -> MUST block
        _ev(_ckey("B", "EXPIRING"), "RESOLVED", "NOISE", raised=n - 3, resolved=n - 2),
        # the finding's scenario (b): cycle 1 EXPIRING superseded, EXPIRED ACK'd then resolved ACTIONED;
        # cycle 2 has now expired -> the CRITICAL EXPIRED must raise again
        _ev(_ckey("C", "EXPIRING"), "RESOLVED", "SUPERSEDED", raised=n - 75, resolved=n - 61),
        _ev(_ckey("C", "EXPIRED"), "RESOLVED", "ACTIONED", raised=n - 61, resolved=n - 60),
        # a live row blocks however old it is (RESOLVED_AT NULL)
        _ev(_ckey("D", "EXPIRING"), "OPEN", raised=n - 100),
        # a same-cycle machine close never blocks (the #12c exemption, kept)
        _ev(_ckey("E", "EXPIRING"), "RESOLVED", "CONDITION_ENDED", raised=n - 3, resolved=n - 1),
        # h-leg: the credential's EXPIRED event is still live -> EXPIRING is never minted
        _ev(_ckey("F", "EXPIRED"), "ACK", raised=n - 20),
        # a same-cycle blank-kind bulk clear (SP_ALERT_CLEAR_SCOPE) -> MUST block
        _ev(_ckey("G", "EXPIRING"), "RESOLVED", None, raised=n - 3, resolved=n - 2),
        # the window edge (opens at n + 5 - 14 = n - 9): EXPECTED just inside blocks, just before does not
        _ev(_ckey("I", "EXPIRING"), "RESOLVED", "EXPECTED", raised=n - 9.5, resolved=n - 8.99),
        _ev(_ckey("J", "EXPIRING"), "RESOLVED", "EXPECTED", raised=n - 9.5, resolved=n - 9.01),
    ]
    rows = _run_arm(_ARM10, {"ALERT_CONFIG": [_cfg("SEC_CRED_EXPIRY")], "CREDENTIALS": creds,
                             "ALERT_EVENTS": events}, n)
    by_key = {r["DEDUPE_KEY"]: r for r in rows}
    assert set(by_key) == {_ckey("A", "EXPIRING"), _ckey("C", "EXPIRED"), _ckey("E", "EXPIRING"),
                           _ckey("J", "EXPIRING")}, sorted(by_key)
    assert by_key[_ckey("C", "EXPIRED")]["SEVERITY"] == "CRITICAL"          # pages + auto-declares again
    assert by_key[_ckey("A", "EXPIRING")]["SEVERITY"] == "HIGH"
    assert by_key[_ckey("A", "EXPIRING")]["TITLE"] == "SVC_A programmatic_access_token 'TOK_A' expires in 5 day(s)"


def test_v157_arm10_a_rotated_credential_re_alerts_through_a_whole_second_cycle():
    """End to end through arm [10] + the V067 supersede token: cycle 1 human-resolved, cycle 2 raises
    EXPIRING at T-days and the CRITICAL EXPIRED at expiry (the finding's stuck-EXPIRING path)."""
    exp1 = _NOW - 90
    ev = [_ev(_ckey("K", "EXPIRING"), "RESOLVED", "SUPERSEDED", raised=exp1 - 14, resolved=exp1 + 0.1),
          _ev(_ckey("K", "EXPIRED"), "RESOLVED", "ACTIONED", raised=exp1 + 0.1, resolved=exp1 + 2)]
    cfg = [_cfg("SEC_CRED_EXPIRY")]
    exp2 = _NOW + 3                                            # rotated: the next 90-day token expires in 3 days
    first = _run_arm(_ARM10, {"ALERT_CONFIG": cfg, "CREDENTIALS": [_cred("K", exp2)], "ALERT_EVENTS": ev}, _NOW)
    assert [r["DEDUPE_KEY"] for r in first] == [_ckey("K", "EXPIRING")]
    ev += _raised(first, _NOW, "k2-")
    at_expiry = exp2 + 0.2                                     # cycle 2 expires unrotated
    second = _run_arm(_ARM10, {"ALERT_CONFIG": cfg, "CREDENTIALS": [_cred("K", exp2)], "ALERT_EVENTS": ev},
                      at_expiry)
    assert [(r["DEDUPE_KEY"], r["SEVERITY"]) for r in second] == [(_ckey("K", "EXPIRED"), "CRITICAL")]
    # the supersede sweep's hi/lo pairing still matches: same key, band token swapped
    assert "REPLACE(lo.DEDUPE_KEY, '|EXPIRING', '|EXPIRED')" in _H
    assert second[0]["DEDUPE_KEY"] == ev[-1]["DEDUPE_KEY"].replace("|EXPIRING", "|EXPIRED")


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
        r"IFF\(cr\.EXPIRATION_DATE < CURRENT_TIMESTAMP\(\), '(\w+)', '(\w+)'\),?\n", _ARM10)[0])
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


# Test-side goldens of the two condition-ended UPDATEs, independent of the generator (a CE edit made in
# outputs/gen_v157.py regenerates V157 byte-identically, so only a golden held HERE catches it). Compared with
# comments stripped and whitespace collapsed.
_CE_GOLDEN = {
    "SEC_CRED_EXPIRY": """
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'CONDITION_ENDED'
         WHERE ev.STATUS = 'OPEN'
           AND ev.RULE_ID = 'SEC_CRED_EXPIRY'
           AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                              WHERE ENABLED AND AUTO_CLEAR_ENABLED AND THRESHOLD_NUM IS NOT NULL)
           AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())
           AND EXISTS (SELECT 1 FROM SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS)
           AND ev.DEDUPE_KEY NOT IN (
               SELECT k.DEDUPE_KEY
               FROM (
                   SELECT c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || bd.BAND AS DEDUPE_KEY
                   FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c
                   JOIN SNOWFLAKE.ACCOUNT_USAGE.CREDENTIALS cr
                     ON c.RULE_ID = 'SEC_CRED_EXPIRY'
                    AND cr.EXPIRATION_DATE IS NOT NULL
                    AND cr.EXPIRATION_DATE <= DATEADD('day',
                            GREATEST(c.THRESHOLD_NUM, COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM)),
                            CURRENT_TIMESTAMP())
                   CROSS JOIN (SELECT 'EXPIRING' AS BAND UNION ALL SELECT 'EXPIRED') bd
               ) k
               WHERE k.DEDUPE_KEY IS NOT NULL
           );
    """,
    "SEC_NEW_EXPOSURE": """
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS ev
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'CONDITION_ENDED'
         WHERE ev.STATUS = 'OPEN'
           AND ev.RULE_ID = 'SEC_NEW_EXPOSURE'
           AND ev.RULE_ID IN (SELECT RULE_ID FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
                              WHERE ENABLED AND AUTO_CLEAR_ENABLED)
           AND ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())
           AND ev.DEDUPE_KEY IN (
               SELECT 'SEC_NEW_EXPOSURE' || '|' || g.PRIVILEGE || '|' || g.GRANTED_ON || '|' || TO_VARCHAR(g.CREATED_ON)
               FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES g
               WHERE g.GRANTEE_NAME = 'PUBLIC'
                 AND g.CREATED_ON >= DATEADD('day', -400, CURRENT_TIMESTAMP())
               GROUP BY g.PRIVILEGE, g.GRANTED_ON, g.CREATED_ON
               HAVING COUNT_IF(g.DELETED_ON IS NULL) = 0
           );
    """,
}


def _norm_sql(sql: str) -> str:
    return " ".join(re.sub(r"--[^\n]*", "", sql).split())


def test_v157_condition_ended_updates_equal_the_test_side_goldens():
    ups = _ce_updates(_CE)
    assert set(ups) == set(_CE_GOLDEN)
    for rule, golden in _CE_GOLDEN.items():
        assert _norm_sql(ups[rule]) == _norm_sql(golden), rule
    cred, expo = _norm_sql(ups["SEC_CRED_EXPIRY"]), _norm_sql(ups["SEC_NEW_EXPOSURE"])
    # polarity: clear what is NOT still expiring / what IS fully revoked
    assert cred.count("AND ev.DEDUPE_KEY NOT IN (") == 1 and "AND ev.DEDUPE_KEY IN (" not in cred
    assert expo.count("AND ev.DEDUPE_KEY IN (") == 1 and "NOT IN" not in expo
    # the key subquery is arm [10]'s rule, and the clear window is never narrower than the raise window
    assert "ON c.RULE_ID = 'SEC_CRED_EXPIRY'" in cred.split("AND ev.DEDUPE_KEY NOT IN (", 1)[1]
    assert ("cr.EXPIRATION_DATE <= DATEADD('day', GREATEST(c.THRESHOLD_NUM, COALESCE(c.CLEAR_THRESHOLD_NUM, "
            "c.THRESHOLD_NUM)), CURRENT_TIMESTAMP())") in cred


def test_v157_condition_ended_sweep_executed_clears_only_ended_credentials():
    """Raise through arm [10] (so the keys are the arm's own), then run the SEC_CRED_EXPIRY sweep: only an
    OPEN event whose credential left the clear window -- GREATEST(THRESHOLD, CLEAR_THRESHOLD) -- and that
    has dwelt >= 1h resolves CONDITION_ENDED."""
    t0 = _NOW - 2
    cfg = [_cfg("SEC_CRED_EXPIRY", CLEAR_THRESHOLD_NUM=7)]          # narrower clear threshold: GREATEST wins
    at_raise = [_cred("K", _NOW + 10), _cred("L", _NOW + 3), _cred("M", _NOW + 3)]
    rows = _run_arm(_ARM10, {"ALERT_CONFIG": cfg, "CREDENTIALS": at_raise}, t0)
    events = _raised(rows, t0, "e")
    assert {e["DEDUPE_KEY"] for e in events} == {_ckey(u, "EXPIRING") for u in "KLM"}
    events.append(_ev(_ckey("N", "EXPIRING"), "OPEN", raised=_NOW - 0.5 / 24))      # raised 30 min ago
    ids = {e["DEDUPE_KEY"].split("|")[1]: e["EVENT_ID"] for e in events}
    for e in events:
        if e["DEDUPE_KEY"] == _ckey("M", "EXPIRING"):
            e["STATUS"] = "ACK"                                                      # a human is working it
    # now: L and M were rotated to a far expiry, N removed; K is still inside the RAISE window (10 <= 14)
    # though outside the CLEAR threshold alone (10 > 7)
    now_creds = [_cred("K", _NOW + 10), _cred("L", _NOW + 200), _cred("M", _NOW + 200), _cred("Z", _NOW + 400)]
    after = _run_ce({"ALERT_CONFIG": cfg, "CREDENTIALS": now_creds, "ALERT_EVENTS": events}, _NOW)
    state = {u: (after[i]["STATUS"], after[i]["RESOLUTION_KIND"]) for u, i in ids.items()}
    assert state == {"SVC_K": ("OPEN", None), "SVC_L": ("RESOLVED", "CONDITION_ENDED"),
                     "SVC_M": ("ACK", None), "SVC_N": ("OPEN", None)}, state
    assert after[ids["SVC_L"]]["RESOLVED_AT"] == _NOW
    # an EMPTY credentials read, a NULL threshold or the flag off never clears anything
    for tables in ({"ALERT_CONFIG": cfg, "CREDENTIALS": [], "ALERT_EVENTS": events},
                   {"ALERT_CONFIG": [_cfg("SEC_CRED_EXPIRY", THRESHOLD_NUM=None)], "CREDENTIALS": now_creds,
                    "ALERT_EVENTS": events},
                   {"ALERT_CONFIG": [_cfg("SEC_CRED_EXPIRY", AUTO_CLEAR_ENABLED=0)], "CREDENTIALS": now_creds,
                    "ALERT_EVENTS": events}):
        assert all(r["RESOLUTION_KIND"] is None for r in _run_ce(tables, _NOW).values())
    # and the arm never re-raises what the sweep cleared (L is outside the raise window now)
    assert not _run_arm(_ARM10, {"ALERT_CONFIG": cfg, "CREDENTIALS": now_creds,
                                 "ALERT_EVENTS": list(after.values())}, _NOW + 1 / 24)


def test_v157_condition_ended_sweep_executed_clears_only_fully_revoked_exposure():
    """Raise through arm [20], then revoke: a batch whose EVERY grant carries DELETED_ON clears; a partly
    revoked or untouched batch, and a key matching no grant row, stay OPEN."""
    t0 = _NOW - 2

    def grant(priv, on, name, created, deleted=None):
        return {"GRANTEE_NAME": "PUBLIC", "PRIVILEGE": priv, "GRANTED_ON": on, "NAME": name,
                "GRANTED_BY": "SYSADMIN", "CREATED_ON": created, "DELETED_ON": deleted}

    c1, c2, c3 = t0 - 0.20, t0 - 0.30, t0 - 0.40
    cfg = [_cfg("SEC_NEW_EXPOSURE", THRESHOLD_NUM=1)]
    at_raise = [grant("SELECT", "TABLE", "T1", c1), grant("SELECT", "TABLE", "T2", c1),
                grant("USAGE", "SCHEMA", "S1", c2),
                grant("SELECT", "VIEW", "V1", c3), grant("SELECT", "VIEW", "V2", c3)]
    rows = _run_arm(_ARM20, {"ALERT_CONFIG": cfg, "GRANTS_TO_ROLES": at_raise}, t0)
    assert len(rows) == 3 and all(r["DEDUPE_KEY"].startswith("SEC_NEW_EXPOSURE|") for r in rows)
    events = [*_raised(rows, t0, "x"),
              _ev("SEC_NEW_EXPOSURE|OWNERSHIP|TABLE|1999-01-01 00:00:00", "OPEN", raised=t0)]
    batch = {"|".join(e["DEDUPE_KEY"].split("|")[1:3]): e["EVENT_ID"] for e in events[:3]}
    gone = _NOW - 1
    now_grants = [grant("SELECT", "TABLE", "T1", c1, gone), grant("SELECT", "TABLE", "T2", c1, gone),
                  grant("USAGE", "SCHEMA", "S1", c2),
                  grant("SELECT", "VIEW", "V1", c3, gone), grant("SELECT", "VIEW", "V2", c3)]
    after = _run_ce({"ALERT_CONFIG": cfg, "GRANTS_TO_ROLES": now_grants, "ALERT_EVENTS": events}, _NOW)
    assert after[batch["SELECT|TABLE"]]["RESOLUTION_KIND"] == "CONDITION_ENDED"
    for b in ("USAGE|SCHEMA", "SELECT|VIEW"):
        assert after[batch[b]]["STATUS"] == "OPEN", b
    assert after["SEC_NEW_EXPOSURE|OWNERSHIP|TABLE|1999-01-01 00:00:00"]["STATUS"] == "OPEN"


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
    # review fix: the formula lines themselves (whitespace-normalized), not just the constants they use
    norm = " ".join(a.split())
    for frag in (
            "GREATEST(i.IDLE_CREDITS - GREATEST(COALESCE(i.METERED_HOURS, 0) - i.IDLE_HOURS, 0) * (60 / 3600.0) "
            "* COALESCE(i.TOTAL_CREDITS / NULLIF(i.METERED_HOURS, 0), 0), 0) AS RECOVERABLE_CREDITS",
            "ROUND(s.RECOVERABLE_CREDITS * :credit_price / s.COVERED_DAYS * 30, 2) AS MONTHLY_USD",
            "cov AS ( SELECT COUNT(DISTINCT DAY) AS COVERED_DAYS FROM win ),",
            "FROM cfg c JOIN opp o ON c.RULE_ID = 'COST_IDLE_OPPORTUNITY' AND o.MONTHLY_USD >= c.THRESHOLD_NUM "
            "CROSS JOIN clk k ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY) "
            "WHERE NOT EXISTS ( SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e "
            "WHERE e.DEDUPE_KEY = b.DEDUPE_KEY ); EXCEPTION"):
        assert norm.count(frag) == 1, frag


# Test-side golden of [24]'s win -> opp CTE chain, independent of the generator (compared normalized).
_ARM24_CHAIN_GOLDEN = """
        win AS (
            SELECT WAREHOUSE_NAME, DAY, BILLED_HOURS, ACTIVE_HOURS, CREDITS_TOTAL,
                   COALESCE(IDLE_CREDITS, CREDITS_TOTAL * COALESCE(IDLE_PCT, 0) / 100) AS IDLE_CR
            FROM DBA_MAINT_DB.OVERWATCH.MART_WAREHOUSE_EFFICIENCY_DAILY
            CROSS JOIN clk
            WHERE DAY >= DATEADD('day', -14, clk.TODAY) AND DAY < clk.TODAY
              AND UPPER(WAREHOUSE_NAME) <> 'CLOUD_SERVICES_ONLY'
        ),
        cov AS (
            SELECT COUNT(DISTINCT DAY) AS COVERED_DAYS FROM win
        ),
        idle AS (
            SELECT WAREHOUSE_NAME,
                   SUM(BILLED_HOURS) AS METERED_HOURS,
                   GREATEST(SUM(BILLED_HOURS) - SUM(ACTIVE_HOURS), 0) AS IDLE_HOURS,
                   SUM(CREDITS_TOTAL) AS TOTAL_CREDITS,
                   SUM(IDLE_CR) AS IDLE_CREDITS
            FROM win
            GROUP BY WAREHOUSE_NAME
            HAVING SUM(CREDITS_TOTAL) > 0
        ),
        scored AS (
            SELECT i.WAREHOUSE_NAME, i.TOTAL_CREDITS, i.IDLE_CREDITS, v.COVERED_DAYS,
                   ROUND(i.IDLE_CREDITS / i.TOTAL_CREDITS * 100, 1) AS IDLE_PCT,
                   GREATEST(i.IDLE_CREDITS
                            - GREATEST(COALESCE(i.METERED_HOURS, 0) - i.IDLE_HOURS, 0) * (60 / 3600.0)
                              * COALESCE(i.TOTAL_CREDITS / NULLIF(i.METERED_HOURS, 0), 0), 0) AS RECOVERABLE_CREDITS
            FROM idle i
            CROSS JOIN cov v
        ),
        newest AS (
            SELECT MAX(SNAPSHOT_AT) AS BATCH_AT
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT
            WHERE SNAPSHOT_AT >= DATEADD('hour', -36, CURRENT_TIMESTAMP())
        ),
        cur AS (
            SELECT s.WAREHOUSE_NAME, s.AUTO_SUSPEND, s.SNAPSHOT_AT
            FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CONFIG_SNAPSHOT s
            JOIN newest n ON s.SNAPSHOT_AT >= DATEADD('minute', -10, n.BATCH_AT)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY UPPER(s.WAREHOUSE_NAME) ORDER BY s.SNAPSHOT_AT DESC) = 1
        ),
        opp AS (
            SELECT s.WAREHOUSE_NAME, s.TOTAL_CREDITS, s.IDLE_CREDITS, s.COVERED_DAYS, s.IDLE_PCT,
                   s.RECOVERABLE_CREDITS, w.AUTO_SUSPEND, w.SNAPSHOT_AT,
                   ROUND(s.RECOVERABLE_CREDITS * :credit_price / s.COVERED_DAYS * 30, 2) AS MONTHLY_USD,
                   GREATEST(30, LEAST(IFF(w.AUTO_SUSPEND > 0, LEAST(w.AUTO_SUSPEND, 60), 60), 3600)) AS TARGET_SEC
            FROM scored s
            JOIN cur w ON UPPER(w.WAREHOUSE_NAME) = UPPER(s.WAREHOUSE_NAME)
            WHERE s.COVERED_DAYS >= 7
              AND s.IDLE_PCT >= 20 AND s.IDLE_CREDITS >= 1
              AND w.AUTO_SUSPEND IS NOT NULL
              AND (w.AUTO_SUSPEND <= 0 OR w.AUTO_SUSPEND > 60)
        )
"""


def test_v157_arm24_cte_chain_equals_the_test_side_golden():
    chain = _ARM24[_ARM24.index("        win AS (\n"):_ARM24.index("        SELECT b.RULE_ID")]
    assert " ".join(chain.split()) == " ".join(_ARM24_CHAIN_GOLDEN.split())
    # the batch anchor is a JOIN, never a nested scalar subquery (Snowflake 002031)
    assert "(SELECT MAX(" not in _ARM24 and "(SELECT MIN(" not in _ARM24


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
    assert "JOIN newest n ON s.SNAPSHOT_AT >= DATEADD('minute', -10, n.BATCH_AT)" in a
    assert "SNOWFLAKE.ACCOUNT_USAGE" not in a
    assert "about the ACTIONABLE figure Optimize shows with a 14-day window; this alert uses 14 " in a
    # V153: the autobook ADOPTS the drawer's $0 closed-loop row (it is the booking of record, not dropped)
    assert "(a $0 closed-loop row booked from this alert is adopted as that booking, not duplicated)" in a
    assert "is superseded)" not in a
    assert "(no manual ledger row)" not in a and "Idle & sizing ACTIONABLE figure)" not in a
    assert a.count(", 300),") == 1 and "\n                   2000),\n" in a
    assert "$$" not in a


# The fixture every executed [24] test shares. WAREHOUSE: (credits/day, idle credits/day, billed h/day,
# active h/day, AUTO_SUSPEND in the NEWEST snapshot batch; WH_G is only in the PRIOR batch).
_IDLE_SPEC = {
    "WH_A": (10.0, 4.5, 10.0, 6.0, 600),           # 45% idle, 10-minute timer -> actionable
    "WH_B": (10.0, 4.5, 10.0, 6.0, 30),            # already tighter than 60s -> excluded by both
    "WH_C": (10.0, 4.5, 10.0, 6.0, 0),             # never suspends -> actionable (enable a timer)
    "WH_D": (10.0, 4.5, 10.0, 6.0, None),          # unknown timer -> excluded by both
    "WH_E": (10.0, 1.996, 4.0, 3.0, 300),          # 19.96% idle rounds to 20.0 -> FLAGGED; 2.5 credits/h
    "WH_F": (0.5, 0.06, 2.0, 1.0, 600),            # < 1 idle credit in the window -> not flagged
    "WH_G": (10.0, 4.5, 10.0, 6.0, 600),           # dropped/renamed since the prior batch -> never raises
}
_IDLE_TODAY = date(2026, 9, 30)                    # a Wednesday: the ISO-week key is Monday 2026-09-28
_IDLE_NOW = _ts(_IDLE_TODAY, 7.0)                  # the daily scan, ~07:00 Central
_IDLE_BATCH = _ts(_IDLE_TODAY, 6 + 40 / 60)        # today's 06:40 SHOW WAREHOUSES batch
_IDLE_PRICE = 3.68


def _idle_tables(days: int = 14, batch_at: float = _IDLE_BATCH, threshold: float = 100.0,
                 events: list | None = None) -> dict:
    mart = [{"WAREHOUSE_NAME": wh, "DAY": (_IDLE_TODAY - timedelta(days=k)).toordinal(), "CREDITS_TOTAL": c,
             "IDLE_CREDITS": i, "IDLE_PCT": None, "BILLED_HOURS": b, "ACTIVE_HOURS": a}
            for wh, (c, i, b, a, _s) in _IDLE_SPEC.items() for k in range(1, days + 1)]
    # rows outside the window never count: today's partial day and day -15
    mart += [{"WAREHOUSE_NAME": "WH_A", "DAY": (_IDLE_TODAY - timedelta(days=k)).toordinal(),
              "CREDITS_TOTAL": 99.0, "IDLE_CREDITS": 99.0, "IDLE_PCT": None, "BILLED_HOURS": 1.0,
              "ACTIVE_HOURS": 0.0} for k in (0, 15)]
    # the PRIOR daily batch lists every warehouse; the NEWEST lists all but WH_G (one INSERT, but a row may
    # carry a stamp a little before the batch max: WH_A 2 minutes early stays inside the 10-minute tolerance)
    snap = [{"WAREHOUSE_NAME": wh, "AUTO_SUSPEND": s[-1], "SNAPSHOT_AT": batch_at - 1}
            for wh, s in _IDLE_SPEC.items()]
    snap += [{"WAREHOUSE_NAME": wh, "AUTO_SUSPEND": s[-1],
              "SNAPSHOT_AT": batch_at - (2 / 1440 if wh == "WH_A" else 0)}
             for wh, s in _IDLE_SPEC.items() if wh != "WH_G"]
    cfg = [_cfg("COST_IDLE_OPPORTUNITY", SEVERITY="MEDIUM", THRESHOLD_NUM=threshold, AUTO_CLEAR_ENABLED=0)]
    return {"MART_WAREHOUSE_EFFICIENCY_DAILY": mart, "WAREHOUSE_CONFIG_SNAPSHOT": snap, "ALERT_CONFIG": cfg,
            "ALERT_EVENTS": events or [], "SETTINGS": [{"KEY": "CREDIT_PRICE_USD", "VALUE": str(_IDLE_PRICE)}]}


def _idle_app_side(days: int = 14) -> pd.DataFrame:
    """What Optimize > Idle & sizing computes: eff_idle_analysis' aggregate over the same complete days, the
    LIVE SHOW WAREHOUSES timer (WH_G is gone), then insights.idle_advisor."""
    from app.logic.insights import idle_advisor
    rows = [{"WAREHOUSE_NAME": wh, "METERED_HOURS": b * days, "IDLE_HOURS": max(b - a, 0) * days,
             "TOTAL_CREDITS": c * days, "IDLE_CREDITS": i * days}
            for wh, (c, i, b, a, _s) in _IDLE_SPEC.items()]
    agg = pd.DataFrame(rows)
    live = pd.DataFrame([{"WAREHOUSE_NAME": wh, "AUTO_SUSPEND": s[-1]} for wh, s in _IDLE_SPEC.items()
                         if wh != "WH_G"])
    agg = agg.merge(live, on="WAREHOUSE_NAME", how="left")
    agg["AUTO_SUSPEND_KNOWN"] = agg["AUTO_SUSPEND"].notna()
    return idle_advisor(agg, _IDLE_PRICE, days).set_index("WAREHOUSE_NAME")


def _by_wh(rows: list[dict]) -> dict:
    return {r["DEDUPE_KEY"].split("|")[1]: r for r in rows}


def test_v157_arm24_executed_sql_matches_insights_idle_advisor():
    """Formula parity with the Optimize ACTIONABLE figure, SQL vs app: the shipped [24] text is EXECUTED over
    the fixture and must raise exactly the app's ACTIONABLE warehouses at its USD/month (same flag, same
    resume-tail haircut, same run-rate, same settings gate) -- and never a warehouse missing from the newest
    SHOW WAREHOUSES batch."""
    app = _idle_app_side()
    app_actionable = set(app.index[app["ACTIONABLE"].astype(bool)])
    assert app_actionable == {"WH_A", "WH_C", "WH_E"}
    assert app.loc["WH_E", "IDLE_PCT"] == 20.0 and bool(app.loc["WH_E", "FLAGGED"])
    assert not bool(app.loc["WH_F", "FLAGGED"]) and app.loc["WH_B", "ACTION_STATUS"] == "ALREADY TUNED"
    assert app.loc["WH_D", "ACTION_STATUS"] == "VERIFY SETTING"
    assert app.loc["WH_G", "ACTION_STATUS"] == "VERIFY SETTING"          # not in live SHOW WAREHOUSES

    arm = _by_wh(_run_arm(_ARM24, _idle_tables(), _IDLE_NOW, _IDLE_PRICE))
    assert set(arm) == app_actionable, sorted(arm)
    for wh in app_actionable:
        assert abs(arm[wh]["METRIC_VALUE"] - app.loc[wh, "ACTIONABLE_MONTHLY_USD"]) <= 0.05, wh
    # hand-checked anchors (catch a formula drift shared by both sides): WH_A recovers 63 idle credits minus
    # 84 active hours x 60s x 1 credit/h = 61.6 credits over 14 days -> 61.6 x 3.68 / 14 x 30; WH_E (2.5
    # credits/h) 27.944 - 42 x 60s x 2.5 = 26.194 credits -> 206.56
    assert arm["WH_A"]["METRIC_VALUE"] == pytest.approx(485.76, abs=0.01)
    assert arm["WH_E"]["METRIC_VALUE"] == pytest.approx(206.56, abs=0.01)
    assert arm["WH_A"]["DEDUPE_KEY"] == "COST_IDLE_OPPORTUNITY|WH_A|MED|2026-09-28"
    assert arm["WH_A"]["SEVERITY"] == "MEDIUM" and arm["WH_A"]["COMPANY"] == "ALL"
    assert arm["WH_A"]["TITLE"] == "WH_A idle waste ~$486/mo: AUTO_SUSPEND 600s -> 60s"
    assert arm["WH_C"]["TITLE"] == "WH_C idle waste ~$486/mo: AUTO_SUSPEND disabled -> 60s"
    da, dc = arm["WH_A"]["DETAIL"], arm["WH_C"]["DETAIL"]
    assert da.startswith("Trailing 14 complete day(s): 63.0 of 140.0 credits burned in hours with zero queries")
    assert "Timer verified by the daily SHOW WAREHOUSES snapshot at 2026-09-30 06:38." in da
    assert "Fix: ALTER WAREHOUSE WH_A SET AUTO_SUSPEND = 60;" in da
    assert "(a $0 closed-loop row booked from this alert is adopted as that booking, not duplicated)" in da
    assert "never-suspend warehouse is not auto-booked" in dc and "adopted as that booking" not in dc
    from app.logic import navigate
    assert navigate.inline_fix_warehouse("COST_IDLE_OPPORTUNITY", arm["WH_A"]["TITLE"]) == "WH_A"


def test_v157_arm24_executed_gates_threshold_dedupe_coverage_and_batch_age():
    run = lambda **kw: _by_wh(_run_arm(_ARM24, _idle_tables(**kw), _IDLE_NOW, _IDLE_PRICE))  # noqa: E731
    # the USD/month threshold gate: WH_E (~$206) drops out at 300
    assert set(run(threshold=300.0)) == {"WH_A", "WH_C"}
    # the HIGH band (>= 5x threshold) mints its own key and severity
    high = run(threshold=90.0)
    assert high["WH_A"]["SEVERITY"] == "HIGH" and high["WH_A"]["DEDUPE_KEY"].endswith("|HIGH|2026-09-28")
    assert high["WH_E"]["SEVERITY"] == "MEDIUM"
    # dedupe: this ISO week's key blocks (any status); last week's key does not
    seen = [_ev("COST_IDLE_OPPORTUNITY|WH_A|MED|2026-09-28", "RESOLVED", "NOISE", raised=_IDLE_NOW - 1,
                resolved=_IDLE_NOW - 0.5),
            _ev("COST_IDLE_OPPORTUNITY|WH_C|MED|2026-09-21", "RESOLVED", "ACTIONED", raised=_IDLE_NOW - 8,
                resolved=_IDLE_NOW - 7)]
    assert set(run(events=seen)) == {"WH_C", "WH_E"}
    # fewer than 7 covered days never raises (COVERED_DAYS counts DAYS, not warehouse-days)
    assert run(days=6) == {}
    seven = run(days=7)
    assert set(seven) == {"WH_A", "WH_C", "WH_E"}
    # run-rated over the COVERED days: 7 days of the same daily profile is the same USD/month
    assert seven["WH_A"]["METRIC_VALUE"] == pytest.approx(485.76, abs=0.01)
    # no SHOW WAREHOUSES batch in the last 36h -> nothing is settings-verified -> nothing raises
    assert run(batch_at=_IDLE_NOW - 37 / 24) == {}


def _assert_preflight_matches_the_arm(preflight_sql: str) -> None:
    """The generated PREFLIGHT_WAVE2B.sql, EXECUTED, lists exactly the warehouses (USD/month, key) the arm
    raises on the same fixture -- including the newest-batch anchor."""
    tables = _idle_tables()
    cur = _connect(tables, _IDLE_NOW).execute(_to_sqlite(preflight_sql))
    cols = [c[0] for c in cur.description]
    pre = {r["WAREHOUSE_NAME"]: r for r in (dict(zip(cols, row, strict=True)) for row in cur.fetchall())}
    arm = _by_wh(_run_arm(_ARM24, tables, _IDLE_NOW, _IDLE_PRICE))
    assert set(pre) == set(arm) == {"WH_A", "WH_C", "WH_E"}, sorted(pre)
    for wh, row in pre.items():
        assert row["MONTHLY_USD"] == arm[wh]["METRIC_VALUE"] and row["DEDUPE_KEY_PREVIEW"] == arm[wh]["DEDUPE_KEY"]
        assert row["RULE_ENABLED"] == "yes"


def test_v157_preflight_executed_matches_the_arm(tmp_path):
    pre = tmp_path / "PREFLIGHT_WAVE2B.sql"
    result = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v157.py")],
                            env=_gen_env(V157_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pre)),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    _assert_preflight_matches_the_arm(pre.read_text(encoding="utf-8"))


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
    assert "adopted as that booking, not duplicated" in pb and "book it once" in pb     # V153 adopt, no twin
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


def test_v157_ops_pipeline_degraded_investigate_sets_no_entity_filter():
    """Review fix: OPS_PIPELINE_DEGRADED text carries a file name and raw loader errors, not entities. Before
    the carve-out, Investigate wrote a sticky top-bar Database filter of LOADER_CHAIN_CHECK (the STALE leg's
    trailing 'snowflake/loader_chain_check.sql.') or DBA_MAINT_DB (an FQN inside the ERR leg's SQLERRM)."""
    from app.logic import navigate
    assert "'snowflake/loader_chain_check.sql.'" in _ARM22_H          # the STALE leg really ends like this
    assert "COALESCE(LEFT(x.LAST_MSG, 600), '—')" in _ARM22_H           # the ERR leg embeds the raw SQLERRM
    stale = ("MART_TAG_COVERAGE_DAILY is stale: 31h 5m since its last load (limit 30h) Loader-owned freshness "
             "row (SOURCE_FRESHNESS_STATE, status OK). A stalled loader, a suspended task or a failed arm leaves "
             "this row behind while TASK_HISTORY still reads SUCCEEDED. Admin > Migrations & freshness > "
             "Diagnose stale sources; snowflake/loader_chain_check.sql.")
    err = ("mart_load_failed: MART_WAREHOUSE_EFFICIENCY_DAILY failed 2x on 2026-09-30 The loader logged this "
           "and returned normally ... Last at 2026-09-30 06:52: SQL compilation error: Object "
           "'DBA_MAINT_DB.OVERWATCH.MART_X' does not exist or not authorized on WH_ALFA_ADMIN. Admin > "
           "Errors & telemetry (persisted error log).")
    for text in (stale, err):
        assert navigate.investigation_target("OPS_PIPELINE_DEGRADED", text) == {
            "page": "Control Room", "section": "Freshness & replay", "filters": {}}
        assert navigate.investigation_target("ops_pipeline_degraded ", text)["filters"] == {}
    assert navigate.fix_target("OPS_PIPELINE_DEGRADED", err) is None
    # the carve-out is rule-scoped: an entity-bearing rule still extracts from the same text
    assert navigate.investigation_target("PERF_SPILL_GB", err)["filters"] == {
        "warehouse_contains": "WH_ALFA_ADMIN", "database": "DBA_MAINT_DB"}


@pytest.mark.parametrize(("lever", "current", "adopted"), [
    ("AUTO_SUSPEND", 600.0, True), ("AUTO_SUSPEND", 61.0, True),
    ("AUTO_SUSPEND", 0.0, False), ("AUTO_SUSPEND", -1.0, False),
    ("MAX_CLUSTERS", None, True),
])
def test_v157_closed_loop_note_never_promises_an_adopt_the_autobook_never_makes(lever, current, adopted):
    """Review fix: [24] raises for a never-suspend (<=0) warehouse and COST_IDLE_OPPORTUNITY now reaches the
    drawer's inline closed loop. tighten_suspend_plan(wh, 0, True) generates SET AUTO_SUSPEND = 60, and V153's
    ADOPT (like V145's book) needs NEW < OLD -- 60 < 0 is false -- so that $0 row is never adopted; its NOTES
    must say so instead of 'the daily change scan adopts and settles it'. The cap-clusters lever stays
    adoptable."""
    from app.logic import remediation
    note = remediation.closed_loop_note_suffix(lever, current)
    assert ("the daily change scan adopts and settles it on its 14-day measured window." in note) is adopted
    if not adopted:
        assert note == ("; enabling a timer on a never-suspend warehouse is not auto-booked — verify it on the "
                        "Savings ledger.")
    # the direction filter the helper mirrors is the shipped ADOPT/book predicate
    v153 = (_MIGDIR / "V153__ledger_autobook_full_window_settle.sql").read_text(encoding="utf-8")
    assert ("(r.SETTING = 'AUTO_SUSPEND'\n             AND COALESCE(TRY_TO_NUMBER(r.NEW_VALUE), 999999999) < "
            "COALESCE(TRY_TO_NUMBER(r.OLD_VALUE), 0))") in v153


def test_v157_closed_loop_note_wiring_for_a_never_suspend_cost_idle_event():
    from app.logic import remediation
    plan = remediation.tighten_suspend_plan("WH_C", 0, True)            # the drawer's plan for [24]'s <=0 case
    assert plan["stmt"] == "ALTER WAREHOUSE WH_C SET AUTO_SUSPEND = 60;"
    assert "not auto-booked" in remediation.closed_loop_note_suffix("AUTO_SUSPEND", 0.0)
    # an unread timer promises nothing either way; a statement timeout is verified by a proof run
    unread = remediation.closed_loop_note_suffix("AUTO_SUSPEND", None)
    assert "adopts and settles it on" not in unread and "Savings ledger" in unread
    assert remediation.closed_loop_note_suffix("STATEMENT_TIMEOUT", None) == (
        "; verify with a proof run on the Savings ledger.")
    # alerts.py feeds the helper the SAME verified current timer the tighten plan used
    src = _read("app/ui/pages/alerts.py")
    assert "_cl_plan = remediation.tighten_suspend_plan(wh_inline, _cl_cur, _cl_known)" in src
    assert ("remediation.closed_loop_note_suffix(\n"
            "                                                        _cl_lever,\n"
            "                                                        _cl_cur if _cl_lever == \"AUTO_SUSPEND\" "
            "else None))") in src
    assert "adopts and settles it on" not in src.split("_cl_note = (", 1)[1][:600]


def test_v157_rollback_note_turns_the_flag_off_before_re_running_v141():
    """Review fix: reversed, an hourly scan landing between the two hand steps runs V141's UNSCOPED V091
    sweep, which AUTO_CLEARs the SEC events 1h after raise -- and V141's arms never re-raise that key."""
    head = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    rb = head[head.index("-- ROLLBACK (order matters): FIRST"):]
    assert rb.index("switch AUTO_CLEAR_ENABLED off for\n-- SEC_CRED_EXPIRY and SEC_NEW_EXPOSURE") < rb.index(
        "only THEN re-run V141's two procs")
    assert "ROLLBACK: re-run V141's two procs AND" not in _MIG
    runbook = _read("RUNBOOK.md")
    sec = runbook[runbook.index("**Rolling back V157 (order matters).**"):]
    flag_off = ("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG SET AUTO_CLEAR_ENABLED = FALSE "
                "WHERE RULE_ID IN ('SEC_CRED_EXPIRY','SEC_NEW_EXPOSURE');")
    assert sec.index(flag_off) < sec.index("Only THEN re-run V141's")


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


# The RUN_NEXT PART B grid (V157.1) checks these with CONTAINS(GET_DDL('PROCEDURE', ...), '<frag>'): each must
# literally be in the proc it is checked against (GET_DDL returns the stored body, comments included) and stay
# quote-free so it pastes into a SQL literal unchanged. SEC_BREAK_GLASS_USE is the expected-FALSE probe.
_PART_B_FRAGMENTS = {
    "SP_ALERT_SCAN()": ("OPS_PIPELINE_DEGRADED", "SP_SCAN_ETL_CYCLE", "CONDITION_ENDED",
                        "V157: only rules whose still-firing set this sweep recomputes",
                        "ALERT_SCAN_HOURLY heartbeat stamp", "alert scan v12 (V157:",
                        "-b.WIN_DAYS, CONVERT_TIMEZONE("),
    "SP_ALERT_SCAN_DAILY()": ("COST_IDLE_OPPORTUNITY", "OPS_PIPELINE_DEGRADED", "ALERT_SCAN_DAILY heartbeat stamp",
                              "alert scan daily v3 (V157:", "JOIN newest n ON s.SNAPSHOT_AT >="),
}


def test_v157_part_b_get_ddl_fragments_are_in_the_procs():
    for proc, frags in _PART_B_FRAGMENTS.items():
        body = _proc(_MIG, proc)
        for frag in frags:
            assert frag in body and "'" not in frag, (proc, frag)
    assert "SEC_BREAK_GLASS_USE" not in _H                  # H_DEAD_ARM_LEFT must read FALSE


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
