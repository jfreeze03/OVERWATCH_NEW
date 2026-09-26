"""Locks for V157 — the single wave-2 re-derivation of both alert scans (Next-Fifty wave 2b).

Ranks 10a/b/d + 13 + 2 (the ETL-cycle add-on slot) + 12c, merged onto the CURRENT definer of both procs,
V141, plus the wave-2b compile-diet rework (owner decisions D1-D4, D8-D10). Hourly SP_ALERT_SCAN: the dead
arm [15] and the retired COST_CLOUD_SVC_RATIO arm [11] out; ONE Central-hour read gates arms [10]/[20] and
their condition-ended clears to every 4th hour and [22] OPS_PIPELINE_DEGRADED to every 3rd; the [23]
PIPE_ETL_CYCLE add-on in; the V091 auto-clear sweep scoped to its 3 PERF rules; a #12c condition-ended sweep
for SEC_CRED_EXPIRY / SEC_NEW_EXPOSURE; the arm [10] recurrence fix; an [hb] heartbeat as a point UPDATE.
Daily SP_ALERT_SCAN_DAILY (ungated): [22] (byte-identical), [24] COST_IDLE_OPPORTUNITY, [hb]. Tallies
13 -> 12 and 9 -> 11. File level: COST_CLOUD_SVC_RATIO retired the V034 way.

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

# Wave-2b rework cadence gates -- test-side copies, independent of the generator. Every gate wraps ONE
# unchanged block: "    IF (<expr>) THEN   -- V157 cadence gate: <label>\n" ... "    END IF;   -- /V157 cadence gate: <label>\n".
_HOUR_READ = "        SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())) INTO :ct_hour;\n"
_HOUR_DECL_RE = re.compile(r"    ct_hour INT DEFAULT (\d+);[^\n]*\n")
_SEC_EXPR, _OPS_EXPR = "MOD(ct_hour, 4) = 1", "MOD(ct_hour, 3) = 2"
_GATE_OPEN_RE = re.compile(r"^    IF \((MOD\(ct_hour, \d\) = \d)\) THEN   -- V157 cadence gate: ([^\n]*)\n", re.M)
_GATE_CLOSE_RE = re.compile(r"^    END IF;   -- /V157 cadence gate: ([^\n]*)\n", re.M)
_GATE_CLOSE = "    END IF;   -- /V157 cadence gate: "

_ARM22_H = _between(_H, "    -- [22] OPS_PIPELINE_DEGRADED", _GATE_CLOSE + "[22]")
_ARM22_D = _between(_D, "    -- [22] OPS_PIPELINE_DEGRADED", "    -- [24] COST_IDLE_OPPORTUNITY")
_ARM23 = _between(_H, "    -- [23] PIPE_ETL_CYCLE", "    IF (fails > 0) THEN\n")
_ARM24 = _between(_D, "    -- [24] COST_IDLE_OPPORTUNITY", "    -- [17] PIPE_REF_GAP")
_ARM10 = _between(_H, "    -- [10] SEC_CRED_EXPIRY", _GATE_CLOSE + "[10]")
_ARM20 = _between(_H, "    -- [20] SEC_NEW_EXPOSURE", _GATE_CLOSE + "[20]")
_V091 = _between(_H, "    -- [auto-clear sweep] V091:", "    -- [condition-ended sweep]")
_CE = _between(_H, "    -- [condition-ended sweep]", "\n    -- [snooze carry-forward sweep] V117:")
_HB_H = _between(_H, "    -- [hb] scan heartbeat", "    RETURN ")
_HB_D = _between(_D, "    -- [hb] scan heartbeat", "    RETURN ")
_HOUR_BLOCK = _between(_H, "    -- [cadence] V157 compile diet", "    -- [wake] V086:")

_ARM15_V141 = _between(_H141, "    -- [15] SEC_BREAK_GLASS_USE\n", "    -- [17] COST_DEPT_BUDGET_PACE\n")
_ARM11_V141 = _between(_H141, "    -- [11] COST_CLOUD_SVC_RATIO\n", "    -- [14] PIPE_COPY_FAILURES\n")
_RET_H141 = "    RETURN 'alert scan v11 (V091: + auto-clear sweep): ' || (13 - :fails) || '/13 rule blocks ok';\n"
_RET_D141 = ("    RETURN 'alert scan daily v2 (V141: storage-surge/serverless-creep/egress-spike moved off the hourly "
             "scan): ' || (9 - :fails) || '/9 rule blocks ok (daily)';\n")
_C1_LINE = ("           AND ev.RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')   "
            "-- V157: only rules whose still-firing set this sweep recomputes; other opt-ins have their own "
            "clear sweep\n")
# arm [10] recurrence fix (#12c + review fixes): the key is unchanged; EXP_TS rides out of b and a CLOSED row
# blocks only when the expiry date at the head of its DETAIL -- the cycle id every arm [10] since V009 writes --
# is THIS expiry's (never by when it was raised or closed). The writer's date and the match are both pinned to
# Central. Test-side copies, independent of the generator.
_H2A_V141 = ("               c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || "
             "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING')\n        FROM cfg c\n")
_H2A_NEW = ("               c.RULE_ID || '|' || cr.USER_NAME || '|' || cr.NAME || '|' || "
            "IFF(cr.EXPIRATION_DATE < CURRENT_TIMESTAMP(), 'EXPIRED', 'EXPIRING'),\n"
            "               cr.EXPIRATION_DATE    -- V157: EXP_TS (this cycle's expiry: the dedupe's cycle id, not "
            "inserted)\n"
            "        FROM cfg c\n")
_H2B_V141 = "        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)\n        WHERE NOT EXISTS (\n"
_H2B_NEW = ("        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY, EXP_TS)\n"
            "        WHERE NOT EXISTS (\n")
_H2D_V141 = "               'Rotate before ' || TO_VARCHAR(cr.EXPIRATION_DATE, 'YYYY-MM-DD') ||\n"
_H2D_NEW = ("               -- V157: this date is the cycle id the dedupe below matches; pinned to Central so a hand-run "
            "scan in another\n"
            "               -- session timezone writes the same date the scheduled scans always have\n"
            "               'Rotate before ' || TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', cr.EXPIRATION_DATE)"
            "::TIMESTAMP_NTZ, 'YYYY-MM-DD') ||\n")
_H2_CYCLE = ("              AND (e.RESOLVED_AT IS NULL\n"
             "                   OR e.DETAIL LIKE ('Rotate before ' || TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', "
             "b.EXP_TS)::TIMESTAMP_NTZ, 'YYYY-MM-DD') || '%'))\n")
_H2_V141 = "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n        );\n"
_H2_NEW = (
    "            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY\n"
    "              AND COALESCE(e.RESOLUTION_KIND, '') NOT IN ('CONDITION_ENDED', 'SUPERSEDED')   "
    "-- V157: a machine close never blocks\n"
    "              -- V157: the key has no date, so the cycle id is the expiry date every arm [10] since V009 writes "
    "at the head\n"
    "              -- of DETAIL (Rotate before YYYY-MM-DD, both bands). A CLOSED row (by anyone: ACTIONED, NOISE, "
    "EXPECTED, a bulk\n"
    "              -- clear, however late) blocks only when it was raised for THIS expiry, never by when it was "
    "raised or closed,\n"
    "              -- so a rotated credential's next expiry re-alerts. A live row (RESOLVED_AT NULL) always blocks.\n"
    + _H2_CYCLE +
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
    retire_row = _MIG.index("DELETE FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n WHERE RULE_ID = 'COST_CLOUD_SVC_RATIO';")
    retire_events = _MIG.index("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS\n   SET STATUS = 'RESOLVED', "
                               "RESOLUTION_KIND = 'EXPECTED'")
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    # the retirement runs after both procs are replaced (no scan can raise [11] any more), row before events
    assert guard < seed < mark_h < create_h < mark_d < create_d < opt_in < retire_row < retire_events < version


def test_v157_is_two_procs_one_seed_one_opt_in_one_retirement_no_task_no_top_level_call():
    from tests.test_migrations_parse import _plain_statements
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 2
    top = "".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0)
    for banned in ("CREATE TASK", "ALTER TASK", "EXECUTE TASK", "CALL ", "CREATE OR REPLACE VIEW",
                   "CREATE TABLE", "ALTER TABLE", "DROP "):
        assert banned not in top, banned
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 2)[:2] for s in _plain_statements(_MIG)]
    assert kinds == [["MERGE", "INTO"], ["UPDATE", "DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG"],
                     ["DELETE", "FROM"], ["UPDATE", "DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"],
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
    # rework: the Central-hour declaration + its one read (the [cadence] block before [wake])
    (decl,) = _HOUR_DECL_RE.findall(h)
    h = _HOUR_DECL_RE.sub("", h, count=1)
    i, j = h.index("    -- [cadence] V157 compile diet"), h.index("    -- [wake] V086:")
    assert h[i:j].count(_HOUR_READ) == 1
    h = h[:i] + h[j:]                                                            # [cadence] hour read
    i, j = h.index(_GATE_OPEN_RE.search(h[h.index("    -- [21] SEC_POSTURE_METRIC"):]).group(0)),\
        h.index("    IF (fails > 0) THEN\n")
    assert "    -- [22] OPS_PIPELINE_DEGRADED" in h[i:j] and "    -- [23] PIPE_ETL_CYCLE" in h[i:j]
    h = h[:i] + h[j:]                                                            # gated [22] + [23]
    i, j = h.index("\n    -- [condition-ended sweep]"), h.index("\n    -- [snooze carry-forward sweep] V117:")
    h = h[:i] + h[j:]                                                            # condition-ended sweep (+ gates)
    # the two security-arm gates: exactly one IF line and one END IF line around each UNCHANGED arm
    opens, closes = _GATE_OPEN_RE.findall(h), _GATE_CLOSE_RE.findall(h)
    assert [e for e, _ in opens] == [_SEC_EXPR, _SEC_EXPR] and [lb for _, lb in opens] == closes
    h = _GATE_CLOSE_RE.sub("", _GATE_OPEN_RE.sub("", h))                         # [10] / [20] gates
    assert "ct_hour" not in h, "every ct_hour use is a declared delta"
    assert h.count("' of 12 alert rule block(s) failed this run'") == 1
    h = h.replace("' of 12 alert rule block(s) failed this run'",
                  "' of 13 alert rule block(s) failed this run'")               # self-alert tally
    assert h.count(_C1_LINE) == 1
    h = h.replace(_C1_LINE, "", 1)                                               # V091 scope line
    assert h.count(_H2_NEW) == 1 and h.count(_H2A_NEW) == 1 and h.count(_H2B_NEW) == 1
    assert h.count(_H2D_NEW) == 1
    h = h.replace(_H2_NEW, _H2_V141, 1)                                          # arm [10] recurrence fix
    h = h.replace(_H2A_NEW, _H2A_V141, 1).replace(_H2B_NEW, _H2B_V141, 1)          # + carried EXP_TS
    h = h.replace(_H2D_NEW, _H2D_V141, 1)                                        # + DETAIL date pinned
    i = h.index("    -- [hb] scan heartbeat")
    j = h.index("\n", h.index("    RETURN ", i)) + 1
    h = h[:i] + _RET_H141 + h[j:]                                                # [hb] + RETURN
    h = h.replace("    -- [17] COST_DEPT_BUDGET_PACE\n", _ARM15_V141 + "    -- [17] COST_DEPT_BUDGET_PACE\n", 1)
    h = h.replace("    -- [14] PIPE_COPY_FAILURES\n", _ARM11_V141 + "    -- [14] PIPE_COPY_FAILURES\n", 1)
    assert h == _H141
    assert decl == "5"


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


def test_v157_retired_ratio_arm_and_its_metering_read_are_gone():
    """Rework D3: arm [11] COST_CLOUD_SVC_RATIO leaves both scans with the hourly scan's only
    WAREHOUSE_METERING_HISTORY read; nothing else in either scan names the rule."""
    for body in (_H, _D):
        assert "COST_CLOUD_SVC_RATIO" not in body and "    -- [11]" not in body
    assert "SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY" not in _H
    assert _ARM11_V141.count(_FAILS_INC) == 1 and "ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY" in _ARM11_V141
    assert _H141.count("ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY") == 1          # it WAS the only one


def test_v157_tallies_equal_the_counting_arms():
    # V141's 13 hourly counting arms - [15] - [11] + [22] = 12; a gated-off arm is skipped whole, EXCEPTION
    # handler included, so it can never increment fails (it counts as ok)
    assert _H.count(_FAILS_INC) == 12 and _D.count(_FAILS_INC) == 11
    assert _H141.count(_FAILS_INC) == 13 and _ARM15_V141.count(_FAILS_INC) == _ARM11_V141.count(_FAILS_INC) == 1
    assert "' of 12 alert rule block(s) failed this run'" in _H and " of 13 " not in _H
    assert "(12 - :fails) || '/12 rule blocks ok'" in _H and "/13 " not in _H
    assert "' of 11 daily alert rule block(s) failed this run'" in _D
    assert "(11 - :fails) || '/11 rule blocks ok (daily)'" in _D
    assert _ARM22_H.count(_FAILS_INC) == 1 and _ARM24.count(_FAILS_INC) == 1
    for block in (_ARM23, _CE, _HB_H, _HB_D, _HOUR_BLOCK):
        assert _FAILS_INC not in block and "fails :=" not in block
    from tests.test_alert_rule_consistency import _FAILS_INC_RE, _SCAN_DENOMINATORS
    for proc, body, n in (("SP_ALERT_SCAN", _H, 12), ("SP_ALERT_SCAN_DAILY", _D, 11)):
        assert len(_FAILS_INC_RE.findall(body)) == n
        self_alert_re, return_re = _SCAN_DENOMINATORS[proc]
        assert {int(x) for x in re.findall(self_alert_re, body)} == {n}
        assert {(int(a), int(b)) for a, b in re.findall(return_re, body)} == {(n, n)}   # RETURN + [hb] STATUS


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
    # the hour is read once, right after the SETTINGS read and before [wake] and every arm
    assert (_H.index("    FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;\n") < _H.index(_HOUR_READ)
            < _H.index("    -- [wake] V086:") < _H.index("    -- [01] COST_DAILY_CREDITS"))


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
                 _H2_CYCLE, "DEDUPE_KEY, EXP_TS)", "b.EXP_TS"):
        assert _H.count(frag) == 1 and frag in _ARM10, frag
    assert _H.count("'Rotate before '") == _ARM10.count("'Rotate before '") == 2      # the writer + the match
    assert _H2_NEW in _ARM10 and _H2A_NEW in _ARM10 and _H2B_NEW in _ARM10 and _H2D_NEW in _ARM10
    assert "WIN_DAYS" not in _MIG                    # the close-time window of round 1 is gone, not dead code
    # the cycle id the dedupe matches is the writer's own DETAIL date expression (same text, same Central pin)
    written = re.search(r"'Rotate before ' \|\| (TO_VARCHAR\(.*?, 'YYYY-MM-DD'\)) \|\|\n", _ARM10).group(1)
    assert written == ("TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', cr.EXPIRATION_DATE)::TIMESTAMP_NTZ, "
                       "'YYYY-MM-DD')")
    assert ("e.DETAIL LIKE ('Rotate before ' || " + written.replace("cr.EXPIRATION_DATE", "b.EXP_TS")
            + " || '%'))") in _ARM10
    # EXP_TS feeds the dedupe only: the INSERT still writes exactly the 7 event columns
    assert ("        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY\n"
            "        FROM (\n") in _ARM10
    # correlated subqueries only at top-level AND (error 002031 class): both sit in the outer WHERE
    tail = _ARM10[_ARM10.index(") b (RULE_ID, COMPANY"):]
    assert tail.index("WHERE NOT EXISTS (") < tail.index("AND NOT EXISTS (")
    assert " OR NOT EXISTS" not in tail and " OR EXISTS" not in tail


# -- rework: cadence gates (owner decisions D1/D2/D8/D9/D12) ----------------------------------------------------

_SEC_HOURS = {1, 5, 9, 13, 17, 21}                  # D1/D2/D9: [10], [20] and their condition-ended clears
_OPS_HOURS = {2, 5, 8, 11, 14, 17, 20, 23}          # D8: [22] in the hourly scan


def _gates(body: str) -> list[tuple[str, str, str]]:
    """[(expr, label, the ONE block it wraps)] in body order; every open has its own labelled close."""
    out = []
    for m in _GATE_OPEN_RE.finditer(body):
        close = _GATE_CLOSE + m.group(2) + "\n"
        out.append((m.group(1), m.group(2), body[m.end():body.index(close, m.end())]))
    assert len(_GATE_CLOSE_RE.findall(body)) == len(out) == body.count("cadence gate: ") // 2
    return out


def _gate_hours(expr: str) -> set[int]:
    """EXECUTE a gate condition for every Central hour 0-23 (ct_hour bound; Snowflake MOD of non-negative
    integers is Python's %)."""
    con = sqlite3.connect(":memory:")
    con.create_function("MOD", 2, lambda a, b: a % b, deterministic=True)
    return {h for h in range(24) if con.execute("SELECT " + expr.replace("ct_hour", "?"), (h,)).fetchone()[0]}


def test_v157_cadence_gates_select_exactly_the_intended_central_hours():
    assert _gate_hours(_SEC_EXPR) == _SEC_HOURS and _gate_hours(_OPS_EXPR) == _OPS_HOURS
    gates = _gates(_H)
    heads = [(expr, blk.split("\n", 1)[0]) for expr, _lbl, blk in gates]
    assert heads == [(_SEC_EXPR, "    -- [10] SEC_CRED_EXPIRY"),
                     (_SEC_EXPR, "    -- [20] SEC_NEW_EXPOSURE (V084 - CoCo Sec36: a new grant to PUBLIC widens "
                                 "the blast radius)"),
                     (_OPS_EXPR, "    -- [22] OPS_PIPELINE_DEGRADED (V157, Next-Fifty #10: OVERWATCH watches its "
                                 "own pipeline from inside BOTH"),
                     (_SEC_EXPR, "    BEGIN"), (_SEC_EXPR, "    BEGIN")], heads
    for _expr, label, blk in gates:
        # each gate wraps ONE isolated block, EXCEPTION handler included: a skipped arm never runs its
        # `fails := fails + 1`, so a gated-off arm counts as ok
        assert blk.endswith("    END;\n") and blk.count("\n    BEGIN\n") + blk.startswith("    BEGIN\n") == 1, label
        assert blk.count(_FAILS_INC) == (0 if "condition-ended" in blk or "clear rides" in label else 1), label
        assert "ct_hour" not in blk, label                               # the gate sits AROUND the block
    # D9: each rule's clear rides exactly its raise arm's gate
    ce = {lbl.split(" ", 1)[0]: expr for expr, lbl, _b in gates if "clear rides" in lbl}
    assert ce == {"SEC_CRED_EXPIRY": gates[0][0], "SEC_NEW_EXPOSURE": gates[1][0]}
    for _expr, lbl, blk in gates[3:]:
        assert f"e.RULE_ID = '{lbl.split(' ', 1)[0]}' AND e.STATUS = 'OPEN'" in blk    # still behind EXISTS-OPEN
    # the labels state the hours their expression selects
    for expr, lbl, _b in gates:
        want = _SEC_HOURS if expr == _SEC_EXPR else _OPS_HOURS
        assert lbl.endswith("(" + ",".join(f"{h:02d}" for h in sorted(want)) + " Central)"), lbl


def test_v157_the_central_hour_is_read_once_and_fails_open():
    code = re.sub(r"--[^\n]*", "", _H)                                   # comments out
    # declared once with DEFAULT 5, read by ONE SELECT ... INTO, never reassigned, read only by the 5 gates
    (default,) = _HOUR_DECL_RE.findall(_H)
    assert int(default) in _SEC_HOURS and int(default) in _OPS_HOURS    # a failed read runs EVERY gated block
    assert _H.count(_HOUR_READ) == 1 and "ct_hour :=" not in _H and code.count("INTO :ct_hour") == 1
    assert code.count("ct_hour") == 1 + 1 + 5
    assert "HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP()))" in _HOUR_READ    # the D12 convention
    # the read is isolated: a failure logs cadence_gate_failed and never touches :fails
    assert _HOUR_BLOCK.count("    BEGIN\n") == 1 and "'cadence_gate_failed'" in _HOUR_BLOCK
    assert _FAILS_INC not in _HOUR_BLOCK and "RETURN" not in _HOUR_BLOCK
    # the daily scan runs once a day: no hour, no gate; its [22] copy is ungated
    assert "ct_hour" not in _D and "cadence gate" not in _D
    assert _D[:_D.index(_ARM22_D)].endswith("    END;\n")                   # [19]'s end, not a gate line


def test_v157_ungated_hourly_arms_run_every_hour():
    gated = [(m.start(), _H.index(_GATE_CLOSE + m.group(2), m.end())) for m in _GATE_OPEN_RE.finditer(_H)]
    for arm in ("[01] COST_DAILY_CREDITS", "[02] COST_WH_DAILY_CREDITS", "[03] PERF_QUERY_FAIL_PCT",
                "[04] PERF_QUEUED_MINUTES", "[05] PERF_SPILL_GB", "[14] PIPE_COPY_FAILURES",
                "[17] COST_DEPT_BUDGET_PACE", "[18] SEC_NEW_ADMIN_NETWORK", "[21] SEC_POSTURE_METRIC",
                "[23] PIPE_ETL_CYCLE", "[wake] V086", "[auto-clear sweep] V091", "[snooze carry-forward sweep]",
                "[hb] scan heartbeat"):
        pos = _H.index("    -- " + arm)
        assert not any(a < pos < b for a, b in gated), arm
    assert not any(a < _H.index("    IF (fails > 0) THEN") < b for a, b in gated)          # the self-alert
    assert not any(a < _H.index("    -- V067 #40: supersede") < b for a, b in gated)       # the supersede sweep


def _day_runs(day: date) -> list[int]:
    """Central hour of each TASK_LOAD_HOURLY run on ``day`` (CRON '7 * * * *' America/Chicago), walked in UTC:
    Snowflake runs a wall-clock CRON time twice when the autumn change repeats it and skips it when spring
    removes it."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Chicago")
    t = datetime(day.year, day.month, day.day, 0, 7, tzinfo=ZoneInfo("UTC")) - timedelta(hours=12)
    hours = []
    for _ in range(48):
        local = t.astimezone(tz)
        if local.date() == day:
            hours.append(local.hour)       # HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP()))
        t += timedelta(hours=1)
    return hours


@pytest.mark.parametrize(("day", "sec", "ops"), [
    (date(2026, 9, 30), 6, 8),             # an ordinary day: 24 runs
    (date(2026, 11, 1), 7, 8),             # autumn change: 01:07 runs twice -> one extra security slot
    (date(2027, 3, 14), 6, 7),             # spring change: 02:07 does not exist -> one [22] slot skipped
])
def test_v157_cadence_model_runs_per_central_day(day, sec, ops):
    """The per-day run counts the report's compile arithmetic uses (and the DST edges the header discloses)."""
    runs = _day_runs(day)
    assert len(runs) == 24 + (sec - 6) - (8 - ops)
    assert sum(_gate_hours(_SEC_EXPR).__contains__(h) for h in runs) == sec
    assert sum(_gate_hours(_OPS_EXPR).__contains__(h) for h in runs) == ops


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
    # ungated here (one CALL statement; SP_SCAN_ETL_CYCLE applies its own ETL-window gate, V156): the line
    # before it closes [22]'s gate and nothing about it reads the hour
    assert "ct_hour" not in a and _H[:_H.index(a)].endswith(_GATE_CLOSE + "[22] every 3h (02,05,08,11,14,17,20,23 "
                                                              "Central)\n")


# -- [hb] heartbeats -----------------------------------------------------------------------------------

@pytest.mark.parametrize(("hb", "source", "total", "status"), [
    (_HB_H, "ALERT_SCAN_HOURLY", 12, "'alert scan ' || (12 - :fails) || '/12 rule blocks ok'"),
    (_HB_D, "ALERT_SCAN_DAILY", 11, "'alert scan daily ' || (11 - :fails) || '/11 rule blocks ok (daily)'"),
])
def test_v157_heartbeats(hb, source, total, status):
    # rework D10: the cheapest shape -- ONE point UPDATE of the scan's own row; the INSERT runs only when the
    # UPDATE matched no row (first run / deleted row), with the same values and GENERATION 1
    assert "MERGE" not in hb
    upd = ("        UPDATE DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE\n"
           "           SET LAST_LOAD_TS = CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,\n"
           f"               ROW_COUNT = ({total} - :fails),\n"
           "               SNAPSHOT_TS = CURRENT_TIMESTAMP(),\n"
           "               GENERATION = COALESCE(GENERATION, 0) + 1,\n"
           f"               STATUS = {status}\n"
           f"         WHERE SOURCE_NAME = '{source}';\n"
           "        IF (SQLROWCOUNT = 0) THEN\n"
           "            INSERT INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE\n"
           "                (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)\n"
           f"            SELECT '{source}', CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ,\n"
           f"                   ({total} - :fails), 1, {status};\n"
           "        END IF;\n")
    assert hb.count(upd) == 1
    assert hb.count("DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE") == 2      # the UPDATE + its fallback only
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
    "SOURCE_FRESHNESS_STATE": ("SOURCE_NAME", "LAST_LOAD_TS", "ROW_COUNT", "SNAPSHOT_TS", "GENERATION", "STATUS"),
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
        resolved: float | None = None, detail: str | None = None) -> dict:
    return {"EVENT_ID": key, "RULE_ID": key.split("|", 1)[0], "DEDUPE_KEY": key, "STATUS": status,
            "RESOLUTION_KIND": kind, "RAISED_AT": raised, "RESOLVED_AT": resolved, "DETAIL": detail}


def _day(x: float) -> str:
    return _dt(x).strftime("%Y-%m-%d")


def _detail(exp: float) -> str:
    """The DETAIL arm [10] itself writes for a credential expiring at ``exp`` (EXECUTED, never hand-typed), so a
    fixture row carries exactly the cycle id a real row of that cycle carries."""
    (row,) = _run_arm(_ARM10, {"ALERT_CONFIG": [_cfg("SEC_CRED_EXPIRY")], "CREDENTIALS": [_cred("W", exp)]},
                      exp - 1)
    return row["DETAIL"]


_SUPERSEDE = _between(_H, "    -- V067 #40: supersede the lower-severity OPEN event", "\n    -- [auto-clear sweep] V091:")


def _scan_pass(events: list[dict], creds: list[dict], cfg: list[dict], now: float,
               tag: str) -> tuple[list[dict], list[dict]]:
    """One hourly pass over SEC_CRED_EXPIRY in SP_ALERT_SCAN's order, every step EXECUTED from the shipped text:
    arm [10] raises (RAISED_AT = now), then the V067 supersede sweep, then the #12c condition-ended sweep.
    -> (the arm's rows, every event afterwards)."""
    rows = _run_arm(_ARM10, {"ALERT_CONFIG": cfg, "CREDENTIALS": creds, "ALERT_EVENTS": events}, now)
    con = _connect({"ALERT_CONFIG": cfg, "CREDENTIALS": creds,
                    "ALERT_EVENTS": [*events, *_raised(rows, now, tag)]}, now)
    (supersede,) = re.findall(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS lo\n.*?;\n", _SUPERSEDE, re.S)
    for stmt in (supersede, _ce_updates(_CE)["SEC_CRED_EXPIRY"]):
        con.execute(_to_sqlite(stmt))
    cur = con.execute("SELECT * FROM ALERT_EVENTS ORDER BY rowid")
    cols = [c[0] for c in cur.description]
    return rows, [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _act(events: list[dict], event_id: str, at: float, status: str, kind: str | None = None) -> list[dict]:
    """A human ACKs or resolves one event (the app's own transition: RESOLVED stamps RESOLVED_AT + a kind)."""
    out = [dict(e) for e in events]
    (e,) = [e for e in out if e["EVENT_ID"] == event_id]
    e["STATUS"] = status
    if status == "RESOLVED":
        e["RESOLVED_AT"], e["RESOLUTION_KIND"] = at, kind
    return out


def _state(events: list[dict]) -> dict:
    return {e["EVENT_ID"]: (e["STATUS"], e["RESOLUTION_KIND"]) for e in events}


def _keys(rows: list[dict]) -> list[tuple]:
    return [(r["DEDUPE_KEY"], r["SEVERITY"]) for r in rows]


# -- #12c / review fixes: arm [10] dedupe is cycle-aware (EXECUTED) ---------------------------------------------
# The key has no date. Round 2: a CLOSED row's cycle is the expiry date at the head of its DETAIL -- never when
# it was raised or closed -- so neither a prior cycle resolved late nor a credential whose lifetime is at most
# THRESHOLD_NUM days can suppress the next cycle.

def test_v157_arm10_cycle_id_is_the_detail_expiry_date_never_the_close_time():
    """A row closed (by ANYONE -- a human ACTIONED/NOISE/EXPECTED or a blank-kind bulk clear) blocks only when it
    was raised for THIS expiry date, however early or late it was closed; a live row always blocks; a machine
    close never blocks; and the h-leg still never mints EXPIRING while the EXPIRED event is live."""
    n = _NOW
    this = _detail(n + 5)
    assert this == (f"Rotate before {_day(n + 5)} to avoid auth failures for jobs and integrations using this "
                    "credential.")
    creds = [_cred(u, n + 5) for u in "ABDEFGIJKLS"] + [_cred("C", n - 1), _cred("H", n + 30)]
    events = [
        # prior cycle (that token expired at n - 85), resolved ACTIONED by a human -> never blocks (re-alerts)
        _ev(_ckey("A", "EXPIRING"), "RESOLVED", "ACTIONED", raised=n - 99, resolved=n - 95, detail=_detail(n - 85)),
        # the finding: a prior cycle resolved LATE -- inside THIS expiry's window -- is still a prior cycle
        _ev(_ckey("K", "EXPIRING"), "RESOLVED", "ACTIONED", raised=n - 99, resolved=n - 1, detail=_detail(n - 85)),
        # same cycle, resolved NOISE -> MUST block
        _ev(_ckey("B", "EXPIRING"), "RESOLVED", "NOISE", raised=n - 3, resolved=n - 2, detail=this),
        # scenario (b) in one frame (T = 14): C's second 16-day token expired at n - 1 (its window opened n - 15);
        # cycle 1 (expired n - 17) EXPIRING was superseded and its EXPIRED resolved ACTIONED at n - 10, AFTER the
        # new window opened -> the CRITICAL EXPIRED must raise again
        _ev(_ckey("C", "EXPIRING"), "RESOLVED", "SUPERSEDED", raised=n - 31, resolved=n - 16.9,
            detail=_detail(n - 17)),
        _ev(_ckey("C", "EXPIRED"), "RESOLVED", "ACTIONED", raised=n - 16.9, resolved=n - 10, detail=_detail(n - 17)),
        # a LIVE row blocks whatever cycle it was raised for (OPEN / SNOOZED; the ACK'd F below via the h-leg)
        _ev(_ckey("D", "EXPIRING"), "OPEN", raised=n - 100, detail=_detail(n - 85)),
        _ev(_ckey("S", "EXPIRING"), "SNOOZED", raised=n - 100, detail=_detail(n - 85)),
        # a same-cycle machine close never blocks (the #12c exemption, kept)
        _ev(_ckey("E", "EXPIRING"), "RESOLVED", "CONDITION_ENDED", raised=n - 3, resolved=n - 1, detail=this),
        # h-leg: the credential's EXPIRED event is still live -> EXPIRING is never minted
        _ev(_ckey("F", "EXPIRED"), "ACK", raised=n - 20, detail=_detail(n - 20)),
        # a same-cycle blank-kind bulk clear (SP_ALERT_CLEAR_SCOPE) -> MUST block
        _ev(_ckey("G", "EXPIRING"), "RESOLVED", None, raised=n - 3, resolved=n - 2, detail=this),
        # same cycle raised AND closed long before this window opened (n - 9; the threshold was 45 then) -> the
        # close time is irrelevant: it is THIS expiry, so it MUST block
        _ev(_ckey("I", "EXPIRING"), "RESOLVED", "EXPECTED", raised=n - 30, resolved=n - 29, detail=this),
        # the cycle id is the exact date: the adjacent calendar days are other expiries -> never block, however
        # recently they were raised and closed
        _ev(_ckey("J", "EXPIRING"), "RESOLVED", "EXPECTED", raised=n - 2, resolved=n - 1, detail=_detail(n + 4)),
        _ev(_ckey("L", "EXPIRING"), "RESOLVED", "EXPECTED", raised=n - 2, resolved=n - 1, detail=_detail(n + 6)),
    ]
    rows = _run_arm(_ARM10, {"ALERT_CONFIG": [_cfg("SEC_CRED_EXPIRY")], "CREDENTIALS": creds,
                             "ALERT_EVENTS": events}, n)
    by_key = {r["DEDUPE_KEY"]: r for r in rows}
    assert set(by_key) == {_ckey("A", "EXPIRING"), _ckey("K", "EXPIRING"), _ckey("C", "EXPIRED"),
                           _ckey("E", "EXPIRING"), _ckey("J", "EXPIRING"), _ckey("L", "EXPIRING")}, sorted(by_key)
    assert by_key[_ckey("C", "EXPIRED")]["SEVERITY"] == "CRITICAL"          # pages + auto-declares again
    assert by_key[_ckey("C", "EXPIRED")]["DETAIL"] == _detail(n - 1)
    assert by_key[_ckey("A", "EXPIRING")]["SEVERITY"] == "HIGH"
    assert by_key[_ckey("A", "EXPIRING")]["TITLE"] == "SVC_A programmatic_access_token 'TOK_A' expires in 5 day(s)"
    assert all(r["DETAIL"] == this for k, r in by_key.items() if k != _ckey("C", "EXPIRED"))


def test_v157_arm10_scenario_b_fifteen_day_pat_resolved_late_re_raises_the_critical_expired():
    """Round-2 finding, the verifier's scenario (b) EXECUTED pass by pass at the production threshold (V028: 10):
    a 15-day PAT; cycle 1's EXPIRING is SUPERSEDED and its EXPIRED ACK'd; the token is rotated at exp1 + 1
    (exp2 = exp1 + 16) and the EXPIRED resolved ACTIONED only at exp1 + 7 -- after cycle 2's window opened
    (exp2 - 10 = exp1 + 6). Cycle 2 raises EXPIRING once the h-leg clears and the CRITICAL EXPIRED at exp2, and
    the V067 sweep supersedes the stale EXPIRING: nothing is left live but the current CRITICAL."""
    cfg = [_cfg("SEC_CRED_EXPIRY", THRESHOLD_NUM=10)]
    exp1 = _NOW
    exp2 = exp1 + 16
    tok1, tok2 = [_cred("P", exp1)], [_cred("P", exp2)]
    rows, ev = _scan_pass([], tok1, cfg, exp1 - 10 + 0.1, "p1-")
    assert _keys(rows) == [(_ckey("P", "EXPIRING"), "HIGH")]
    rows, ev = _scan_pass(ev, tok1, cfg, exp1 + 0.1, "p2-")
    assert _keys(rows) == [(_ckey("P", "EXPIRED"), "CRITICAL")]
    assert _state(ev) == {"p1-0": ("RESOLVED", "SUPERSEDED"), "p2-0": ("OPEN", None)}
    ev = _act(ev, "p2-0", exp1 + 0.2, "ACK")                          # a human works the auto-declared incident
    rows, ev = _scan_pass(ev, tok2, cfg, exp1 + 1.1, "p3-")          # rotated at exp1 + 1: nothing inside T
    assert rows == [] and _state(ev)["p2-0"] == ("ACK", None)          # CONDITION_ENDED never touches an ACK
    rows, ev = _scan_pass(ev, tok2, cfg, exp1 + 6.5, "p4-")          # cycle 2's window is open ...
    assert rows == []                                                  # ... but EXPIRED is live: the h-leg holds
    still_acked = ev
    ev = _act(ev, "p2-0", exp1 + 7, "RESOLVED", "ACTIONED")           # closed 6 days after the rotation
    rows, ev = _scan_pass(ev, tok2, cfg, exp1 + 7.1, "p5-")
    assert _keys(rows) == [(_ckey("P", "EXPIRING"), "HIGH")]
    assert rows[0]["DETAIL"].startswith(f"Rotate before {_day(exp2)} ")
    rows, ev = _scan_pass(ev, tok2, cfg, exp2 + 0.2, "p6-")          # cycle 2 expires unrotated
    assert _keys(rows) == [(_ckey("P", "EXPIRED"), "CRITICAL")]         # pages + auto-declares again
    assert _state(ev) == {"p1-0": ("RESOLVED", "SUPERSEDED"), "p2-0": ("RESOLVED", "ACTIONED"),
                          "p5-0": ("RESOLVED", "SUPERSEDED"), "p6-0": ("OPEN", None)}
    # a live row always blocks: had cycle 1's EXPIRED stayed ACK'd, the queue keeps THAT event and nothing new
    assert _scan_pass(still_acked, tok2, cfg, exp2 + 0.2, "px-")[0] == []


def test_v157_arm10_a_seven_day_pat_rotated_early_re_raises_its_next_expiry():
    """Round-2 finding, the short-lifetime case: a 7-day PAT (lifetime <= THRESHOLD_NUM 10) is inside the window
    from birth, so no close-time or raise-time window can tell its cycles apart. Rotated at day 2 and resolved
    ACTIONED at the rotation, its next expiry (day 9) must raise EXPIRING at once; that new cycle then dedupes like
    any other (its live row blocks, a same-cycle human close still blocks)."""
    cfg = [_cfg("SEC_CRED_EXPIRY", THRESHOLD_NUM=10)]
    t0 = _NOW                                                          # the first token is created
    exp1, exp2 = t0 + 7, t0 + 9                                        # rotated at t0 + 2 -> the new token
    rows, ev = _scan_pass([], [_cred("Q", exp1)], cfg, t0 + 0.05, "q1-")
    assert _keys(rows) == [(_ckey("Q", "EXPIRING"), "HIGH")]
    ev = _act(ev, "q1-0", t0 + 2, "RESOLVED", "ACTIONED")
    tok2 = [_cred("Q", exp2)]
    for at in (t0 + 2.1, exp2 - 1):                                   # right after the rotation, a day before exp2
        again = _run_arm(_ARM10, {"ALERT_CONFIG": cfg, "CREDENTIALS": tok2, "ALERT_EVENTS": ev}, at)
        assert _keys(again) == [(_ckey("Q", "EXPIRING"), "HIGH")], at
        assert again[0]["DETAIL"].startswith(f"Rotate before {_day(exp2)} ")
    rows, ev = _scan_pass(ev, tok2, cfg, t0 + 2.1, "q2-")
    assert len(rows) == 1
    assert _scan_pass(ev, tok2, cfg, exp2 - 1, "q3-")[0] == []         # the live cycle-2 row blocks
    ev = _act(ev, "q2-0", exp2 - 0.9, "RESOLVED", "NOISE")
    assert _scan_pass(ev, tok2, cfg, exp2 - 0.8, "q4-")[0] == []       # a same-cycle human close still blocks


def test_v157_arm10_a_rotated_credential_re_alerts_through_a_whole_second_cycle():
    """End to end through arm [10] + the V067 supersede token: cycle 1 human-resolved, cycle 2 raises
    EXPIRING at T-days and the CRITICAL EXPIRED at expiry (the finding's stuck-EXPIRING path)."""
    exp1 = _NOW - 90
    ev = [_ev(_ckey("K", "EXPIRING"), "RESOLVED", "SUPERSEDED", raised=exp1 - 14, resolved=exp1 + 0.1,
              detail=_detail(exp1)),
          _ev(_ckey("K", "EXPIRED"), "RESOLVED", "ACTIONED", raised=exp1 + 0.1, resolved=exp1 + 2,
              detail=_detail(exp1))]
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


def _select_items(body: str) -> list[str]:
    """Top-level comma split of a SELECT list (comments stripped; quotes and parentheses respected)."""
    body = re.sub(r"--[^\n]*", "", body)
    items: list[str] = []
    cur: list[str] = []
    depth, quoted = 0, False
    for ch in body:
        if ch == "'":
            quoted = not quoted                    # a doubled '' toggles twice: still correct
        elif not quoted and ch in "()":
            depth += 1 if ch == "(" else -1
        if ch == "," and depth == 0 and not quoted:
            items.append(" ".join("".join(cur).split()))
            cur = []
        else:
            cur.append(ch)
    return [*items, " ".join("".join(cur).split())]


_DETAIL_TAIL = " || ' to avoid auth failures for jobs and integrations using this credential.'"


def test_v157_arm10_cycle_id_is_written_by_every_definer_since_v009():
    """The premise of the cycle id, over the rows history holds: EVERY definer of arm [10] since the rule was
    introduced (V009) writes DETAIL = 'Rotate before ' || <the expiry as YYYY-MM-DD> || one fixed tail, from ONE
    projection for both bands, in the INSERT's DETAIL position -- and nothing else ever rewrites the head of an
    ALERT_EVENTS DETAIL (the other writers only append a suffix) or inserts a SEC_CRED_EXPIRY row."""
    title = "cr.USER_NAME || ' ' || LOWER(cr.TYPE) || ' ''' || cr.NAME || ''' ' ||"
    definers: dict[int, str] = {}
    for f in sorted(_MIGDIR.glob("V*.sql")):
        text = f.read_text(encoding="utf-8")
        heads = [m.start() + 1 for m in re.finditer(r"\n[ ]+'Rotate before ' \|\|", text)]
        assert text.count(title) == len(heads) <= 1, f.name       # one arm [10] per definer, one DETAIL head
        for pos in heads:
            ins = text.rindex("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS", 0, pos)
            cols = _select_items(re.match(r"INSERT INTO DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS\s*\(([^)]*)\)",
                                          text[ins:]).group(1))
            assert cols[:5] == ["RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL"], f.name
            items = _select_items(text[text.rindex("SELECT c.RULE_ID,", 0, pos) + 7:text.index("FROM cfg c", pos)])
            assert title.rstrip(" |") in items[3] and "IFF(" not in items[4], f.name   # one projection, both bands
            if "candidates AS (" in text[ins:pos]:               # V009-V016: a UNION ALL branch named by branch 1
                first = text.index("SELECT ", text.index("candidates AS (", ins))
                assert _select_items(text[first + 7:text.index("FROM cfg c", first)])[4].endswith(" AS DETAIL")
                assert ("SELECT c.RULE_ID, c.COMPANY, c.SEVERITY, c.TITLE, c.DETAIL, c.METRIC_VALUE, c.DEDUPE_KEY\n"
                        "    FROM candidates c") in text[pos:], f.name
            definers[int(f.name[1:4])] = items[4]
    assert min(definers) == 9 and 157 in definers and len(definers) == 30, sorted(definers)
    for v, detail in definers.items():
        dates = ({"TO_VARCHAR(cr.EXPIRES_AT, 'YYYY-MM-DD')", "TO_VARCHAR(cr.EXPIRATION_DATE, 'YYYY-MM-DD')"}
                 if v < 157 else
                 {"TO_VARCHAR(CONVERT_TIMEZONE('America/Chicago', cr.EXPIRATION_DATE)::TIMESTAMP_NTZ, 'YYYY-MM-DD')"})
        assert any(detail == "'Rotate before ' || " + d + _DETAIL_TAIL for d in dates), (v, detail)
    # every other DETAIL writer appends (Cortex pre-explain in the anomaly sweeps; the app's AI hypothesis)
    for f in sorted(_MIGDIR.glob("V*.sql")):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"(?<![\w.])DETAIL\s*=\s*", text):
            assert text[m.end():].startswith("LEFT(COALESCE(DETAIL, '') || "), (f.name, text[m.end():m.end() + 40])
    alerts = _read("app/ui/pages/alerts.py")
    assert alerts.count("SET DETAIL = ") == 1
    assert "f\"LEFT(COALESCE(DETAIL, '') || ' | AI hypothesis: ' || \"" in alerts
    # and no SEC_CRED_EXPIRY row comes from outside a migration: the one other ALERT_EVENTS INSERT is the drill
    sources = [*(_ROOT / "app").rglob("*.py"), *(_ROOT / "snowflake").glob("*.sql")]
    assert {p.relative_to(_ROOT).as_posix() for p in sources
            if re.search(r"INSERT INTO[^\n]*ALERT_EVENTS", p.read_text(encoding="utf-8"))} == {
                "snowflake/alert_drill.sql"}
    assert "SEC_CRED_EXPIRY" not in _read("snowflake/alert_drill.sql")


# -- #12c condition-ended sweep -----------------------------------------------------------------------------

def test_v157_condition_ended_block_shape():
    ce = _CE
    assert ce.startswith("    -- [condition-ended sweep] V157 (Next-Fifty #12c): ")
    assert _H.count("    -- [condition-ended sweep]") == 1
    # rework D9: each rule's isolated block sits WHOLE inside its raise arm's security-slot gate
    for rule, arm in (("SEC_CRED_EXPIRY", "[10]"), ("SEC_NEW_EXPOSURE", "[20]")):
        label = f"{rule} clear rides arm {arm} every 4h (01,05,09,13,17,21 Central)"
        opn = f"    IF ({_SEC_EXPR}) THEN   -- V157 cadence gate: {label}\n"
        blk = _between(ce, opn, _GATE_CLOSE + label)[len(opn):]
        assert ce.count(opn) == 1 and blk.startswith("    BEGIN\n") and blk.endswith("    END;\n"), rule
        assert blk.count("    BEGIN\n") == 1 and f"'V157 condition-ended sweep {rule} - other" in blk, rule
    assert ce.endswith(_GATE_CLOSE + "SEC_NEW_EXPOSURE clear rides arm [20] every 4h (01,05,09,13,17,21 Central)\n")
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


# -- [hb] heartbeat, EXECUTED (rework D10: one point UPDATE; the INSERT only when it matched no row) ----------

def _hb_pass(hb: str, con: sqlite3.Connection, fails: int) -> str:
    """Run one [hb] pass the way Snowflake Scripting does: the UPDATE, then IF (SQLROWCOUNT = 0) the INSERT.
    -> 'UPDATE' or 'INSERT' (which statement landed the stamp)."""
    upd = re.search(r"        UPDATE DBA_MAINT_DB\.OVERWATCH\.SOURCE_FRESHNESS_STATE\n.*?;\n", hb, re.S).group(0)
    ins = re.search(r"            INSERT INTO DBA_MAINT_DB\.OVERWATCH\.SOURCE_FRESHNESS_STATE\n.*?;\n", hb,
                    re.S).group(0)
    if con.execute(_to_sqlite(upd.replace(":fails", str(fails)))).rowcount:
        return "UPDATE"
    con.execute(_to_sqlite(ins.replace(":fails", str(fails))))
    return "INSERT"


@pytest.mark.parametrize(("hb", "source", "status_ok", "status_2"), [
    (_HB_H, "ALERT_SCAN_HOURLY", "alert scan 12/12 rule blocks ok", "alert scan 10/12 rule blocks ok"),
    (_HB_D, "ALERT_SCAN_DAILY", "alert scan daily 11/11 rule blocks ok (daily)",
     "alert scan daily 9/11 rule blocks ok (daily)"),
])
def test_v157_heartbeat_executed_stamps_self_heals_and_touches_only_its_row(hb, source, status_ok, status_2):
    other = {"SOURCE_NAME": "FACT_QUERY_HOURLY", "LAST_LOAD_TS": _NOW - 1, "ROW_COUNT": 7, "GENERATION": 41,
             "STATUS": "OK"}
    con = _connect({"SOURCE_FRESHNESS_STATE": [other]}, _NOW)

    def rows() -> dict:
        cur = con.execute("SELECT * FROM SOURCE_FRESHNESS_STATE ORDER BY rowid")
        cols = [c[0] for c in cur.description]
        return {r[0]: dict(zip(cols, r, strict=True)) for r in cur.fetchall()}

    assert _hb_pass(hb, con, 0) == "INSERT"                          # first run: the row does not exist yet
    got = rows()[source]
    assert (got["LAST_LOAD_TS"], got["ROW_COUNT"], got["GENERATION"], got["STATUS"]) == (_NOW, 12 if "HOURLY" in
                                                                                        source else 11, 1, status_ok)
    con = _connect({"SOURCE_FRESHNESS_STATE": list(rows().values())}, _NOW + 1 / 24)
    assert _hb_pass(hb, con, 2) == "UPDATE"                          # every later run: one point UPDATE
    after = rows()
    assert len(after) == 2 and after["FACT_QUERY_HOURLY"] == dict(other, SNAPSHOT_TS=None)   # untouched
    got = after[source]
    assert (got["LAST_LOAD_TS"], got["GENERATION"], got["STATUS"]) == (_NOW + 1 / 24, 2, status_2)
    assert got["SNAPSHOT_TS"] == _NOW + 1 / 24
    con.execute("DELETE FROM SOURCE_FRESHNESS_STATE WHERE SOURCE_NAME = ?", (source,))
    assert _hb_pass(hb, con, 0) == "INSERT" and rows()[source]["GENERATION"] == 1   # a deleted row self-heals


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


# -- rework: COST_CLOUD_SVC_RATIO retirement (owner decision D3; the V034 house pattern) ------------------------

_RETIRE_ROW = "DELETE FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n WHERE RULE_ID = 'COST_CLOUD_SVC_RATIO';\n"
_RETIRE_EVENTS = ("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS\n"
                  "   SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED',\n"
                  "       RESOLVED_AT = CURRENT_TIMESTAMP()\n"
                  " WHERE RULE_ID = 'COST_CLOUD_SVC_RATIO' AND STATUS IN ('OPEN', 'ACK', 'SNOOZED');\n")


def test_v157_retires_cost_cloud_svc_ratio_the_v034_way():
    assert _MIG.count(_RETIRE_ROW) == 1 and _MIG.count(_RETIRE_EVENTS) == 1
    assert _MIG.index(_RETIRE_ROW) < _MIG.index(_RETIRE_EVENTS)          # row first: nothing re-raises after
    v034 = (_MIGDIR / "V034__route_company_filter.sql").read_text(encoding="utf-8")
    # the same two statements V034 used for SEC_BREAK_GLASS_USE -- the kind EXPECTED, never a new one ...
    assert "SET STATUS = 'RESOLVED', RESOLUTION_KIND = 'EXPECTED',\n       RESOLVED_AT = CURRENT_TIMESTAMP()" in v034
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n WHERE RULE_ID = 'SEC_BREAK_GLASS_USE';" in v034
    # ... + SNOOZED (V086 came later: the hourly wake would reopen a snoozed event no scan can ever close)
    assert " WHERE RULE_ID = 'SEC_BREAK_GLASS_USE' AND STATUS IN ('OPEN', 'ACK');" in v034
    v116 = (_MIGDIR / "V116__alert_clear_scope_proc.sql").read_text(encoding="utf-8")
    assert "IF (v_kind NOT IN ('ACTIONED', 'NOISE', 'EXPECTED')) THEN" in v116     # a kind humans already use
    from tests.test_alert_rule_consistency import (
        RETIRED_ALLOWLIST,
        _app_claims,
        _config_deleted,
        _config_enabled,
        _raised_rule_ids,
    )
    raised, enabled = _raised_rule_ids(), _config_enabled()
    assert "COST_CLOUD_SVC_RATIO" in _config_deleted() and "COST_CLOUD_SVC_RATIO" not in enabled
    assert "COST_CLOUD_SVC_RATIO" not in raised and "COST_CLOUD_SVC_RATIO" in RETIRED_ALLOWLIST
    assert _app_claims().get("COST_CLOUD_SVC_RATIO") == {"RETIRED"}       # the app says so (its playbook)
    assert "COST_CLOUD_SVC_ANOMALY" in raised and "COST_CLOUD_SVC_ANOMALY" in enabled   # the successor is live


def test_v157_retirement_executed_closes_only_the_retired_rules_live_events():
    key = "COST_CLOUD_SVC_RATIO|WH_{}|2026-09-2{}"
    events = [_ev(key.format("A", 9), "OPEN", raised=_NOW - 1), _ev(key.format("B", 9), "ACK", raised=_NOW - 1),
              _ev(key.format("C", 8), "SNOOZED", raised=_NOW - 2),
              _ev(key.format("D", 7), "RESOLVED", "ACTIONED", raised=_NOW - 3, resolved=_NOW - 2.5),
              _ev("COST_CLOUD_SVC_ANOMALY|WH_A|2026-09-29", "OPEN", raised=_NOW - 1),
              _ev("SEC_NEW_EXPOSURE|SELECT|TABLE|2026-09-29 01:00:00", "OPEN", raised=_NOW - 1)]
    cfg = [_cfg("COST_CLOUD_SVC_RATIO"), _cfg("COST_CLOUD_SVC_ANOMALY")]
    con = _connect({"ALERT_EVENTS": events, "ALERT_CONFIG": cfg}, _NOW)
    # EXECUTE the file's own retirement statements, in file order (never the test-side copies above)
    block = _between(_MIG, "-- Owner decision (wave-2b rework): retire COST_CLOUD_SVC_RATIO.",
                     "INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    stmts = [s.strip() for s in re.sub(r"--[^\n]*\n", "", block).split(";") if s.strip()]
    assert [s.split()[0] for s in stmts] == ["DELETE", "UPDATE"]
    for stmt in stmts:
        con.execute(_to_sqlite(stmt))
    assert [r[0] for r in con.execute("SELECT RULE_ID FROM ALERT_CONFIG")] == ["COST_CLOUD_SVC_ANOMALY"]
    cur = con.execute("SELECT EVENT_ID, STATUS, RESOLUTION_KIND, RESOLVED_AT FROM ALERT_EVENTS")
    got = {r[0]: r[1:] for r in cur.fetchall()}
    for wh, day in (("A", 9), ("B", 9), ("C", 8)):
        assert got[key.format(wh, day)] == ("RESOLVED", "EXPECTED", _NOW), wh
    assert got[key.format("D", 7)] == ("RESOLVED", "ACTIONED", _NOW - 2.5)           # history untouched
    assert got["COST_CLOUD_SVC_ANOMALY|WH_A|2026-09-29"] == ("OPEN", None, None)       # other rules untouched
    assert got["SEC_NEW_EXPOSURE|SELECT|TABLE|2026-09-29 01:00:00"] == ("OPEN", None, None)


def test_v157_cadence_and_retirement_are_documented_for_the_operator():
    from app.logic.playbooks import PLAYBOOKS
    ratio = PLAYBOOKS["COST_CLOUD_SVC_RATIO"]
    assert ratio.startswith("**Retired (V157).**") and "closed as EXPECTED" in ratio
    assert "COST_CLOUD_SVC_ANOMALY" not in ratio          # naming a live rule next to 'retired' would trip Guard B
    for rule in ("SEC_CRED_EXPIRY", "SEC_NEW_EXPOSURE"):
        pb = PLAYBOOKS[rule]
        assert "every 4 hours (01, 05, 09, 13, 17 and 21 Central)" in pb, rule
        assert "up to ~4h after" in pb and "next 4-hourly check resolves the OPEN event as CONDITION_ENDED" in pb
    assert "the CRITICAL EXPIRED one that auto-declares an incident" in PLAYBOOKS["SEC_CRED_EXPIRY"]
    assert ("Checked every 3 hours by the hourly scan (02, 05, 08, 11, 14, 17, 20 and 23 Central) and once each "
            "morning by the daily scan") in PLAYBOOKS["OPS_PIPELINE_DEGRADED"]
    rb = _read("RUNBOOK.md")
    assert "**Cadence gates (V157, compile diet):**" in rb
    assert "| ~~COST_CLOUD_SVC_RATIO~~ | COST | retired at V157" in rb
    assert "| COST_CLOUD_SVC_RATIO | COST |" not in rb                          # the live-looking row is gone
    (exp_row,) = [ln for ln in rb.splitlines() if ln.startswith("| SEC_NEW_EXPOSURE | SECURITY |")]
    assert "checked every 4h since V157" in exp_row
    (cred_row,) = [ln for ln in rb.splitlines() if ln.startswith("| SEC_CRED_EXPIRY | SECURITY |")]
    assert "checked every 4h since V157" in cred_row and "EXPIRED included" in cred_row
    (ops_row,) = [ln for ln in rb.splitlines() if ln.startswith("| OPS_PIPELINE_DEGRADED | PLATFORM |")]
    assert "the hourly scan checks every 3h" in ops_row
    # review fix: a condition shorter than the check interval is never raised (not only delayed)
    assert "is never raised" in PLAYBOOKS["SEC_NEW_EXPOSURE"]
    assert "ends between two checks is not raised" in PLAYBOOKS["OPS_PIPELINE_DEGRADED"]
    assert "never raised" in exp_row and "is not raised" in ops_row
    assert "ends between two checks is never raised" in " ".join(rb.split())
    assert "is never raised at all" in _MIG
    # review fix: a hand CALL outside a slot reports 12/12 ok without running the gated rule
    osd = PLAYBOOKS["OPS_SCAN_DEGRADED"]
    assert "still reports 12/12 ok" in osd and "05 or 17 Central hour" in osd
    assert "A hand `CALL SP_ALERT_SCAN()` obeys the same gates" in " ".join(rb.split())
    spend = _read("app/ui/pages/cost_parts/spend.py")
    assert "where the COST_CLOUD_SVC_RATIO alert fires" not in spend and "fixed-ratio alert was retired" in spend
    nav = _read("app/logic/navigate.py")
    assert "stay only so the drawer still routes its historical events" in nav
    sec = _read("app/ui/pages/security.py")
    assert "checked every 4 hours (01, 05, \"\n" in sec and "re-raised weekly until rotated" not in sec


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
    (cred_row,) = [ln for ln in rb.splitlines() if ln.startswith("| SEC_CRED_EXPIRY | SECURITY |")]
    assert "once per band per expiry date" in cred_row and "weekly until rotated" not in cred_row


def test_v157_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")


def test_v157_inserted_select_statements_parse():
    """The new arms' INSERT ... WITH statements parse as Snowflake SQL once the :binds are literals."""
    sqlglot = pytest.importorskip("sqlglot")
    for block in (_ARM22_H, _ARM24, _ARM10):
        stmt = block[block.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):block.index("    EXCEPTION")]
        sqlglot.parse(stmt.replace(":credit_price", "3.68"), dialect="snowflake")
    for gate in re.findall(r"UPDATE DBA_MAINT_DB\.OVERWATCH\.ALERT_EVENTS ev.*?;\n", _CE, re.S):
        sqlglot.parse(gate, dialect="snowflake")


# The RUN_NEXT PART B grid (V157.1) checks these with CONTAINS(GET_DDL('PROCEDURE', ...), '<frag>'): each must
# literally be in the proc it is checked against (GET_DDL returns the stored body, comments included) and stay
# quote-free so it pastes into a SQL literal unchanged (GET_DDL re-quotes the body, so a fragment holding a
# quote never matches). _PART_B_ABSENT are the expected-FALSE probes.
_PART_B_FRAGMENTS = {
    "SP_ALERT_SCAN()": ("OPS_PIPELINE_DEGRADED", "SP_SCAN_ETL_CYCLE", "CONDITION_ENDED",
                        "V157: only rules whose still-firing set this sweep recomputes",
                        "ALERT_SCAN_HOURLY heartbeat stamp", "alert scan v12 (V157:",
                        "OR e.DETAIL LIKE (", "INTO :ct_hour", "IF (MOD(ct_hour, 4) = 1) THEN",
                        "IF (MOD(ct_hour, 3) = 2) THEN", "IF (SQLROWCOUNT = 0) THEN", "/12 rule blocks ok"),
    "SP_ALERT_SCAN_DAILY()": ("COST_IDLE_OPPORTUNITY", "OPS_PIPELINE_DEGRADED", "ALERT_SCAN_DAILY heartbeat stamp",
                              "alert scan daily v3 (V157:", "JOIN newest n ON s.SNAPSHOT_AT >=",
                              "IF (SQLROWCOUNT = 0) THEN"),
}
_PART_B_ABSENT = {
    "SP_ALERT_SCAN()": ("SEC_BREAK_GLASS_USE", "COST_CLOUD_SVC_RATIO", "WAREHOUSE_METERING_HISTORY",
                        "MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE"),
    "SP_ALERT_SCAN_DAILY()": ("ct_hour", "MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE"),
}


def test_v157_part_b_get_ddl_fragments_are_in_the_procs():
    for proc, frags in _PART_B_FRAGMENTS.items():
        body = _proc(_MIG, proc)
        for frag in frags:
            assert frag in body and "'" not in frag, (proc, frag)
        for frag in _PART_B_ABSENT[proc]:
            assert frag not in body and "'" not in frag, (proc, frag)
    # the security gate appears exactly 4 times ([10], [20] and their clears), the [22] gate once
    assert _H.count("IF (MOD(ct_hour, 4) = 1) THEN") == 4 and _H.count("IF (MOD(ct_hour, 3) = 2) THEN") == 1


# -- integration lockstep (validate floor / docs / Admin). Pinned to the WAVE TIP V158; these are completed
#    by the wave-2b integrator (snowflake/validate.sql, DEPLOYMENT.md, README.md and
#    admin._EXPECTED_MIGRATIONS are shared files a single slice does not edit).

def test_validate_and_docs_track_v157():
    val = _read("snowflake/validate.sql")
    assert "V001..V159 applied" in val and "VERSION BETWEEN 1 AND 159) = 159" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert _NAME in _read(rel), rel


def test_v157_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 157 in _EXPECTED_MIGRATIONS
