"""V153 locks: SP_LEDGER_AUTOBOOK settles on the FULL 14-day window (Next-Fifty #11 + the #5 adopt).

Re-derived from V145 -- the CURRENT definer (lineage V038 -> V118 -> V145) -- and SP_VERIFY_IDLE_SAVINGS
from V053. Every delta is reversed here back to the exact base text (normalize-and-compare), which
transitively proves V118's LBA-1 dedup and V038's book/settle core survived. Byte-locked to
outputs/gen_v153.py. Owner decisions implemented at their defaults: O-2 (NO_BASELINE /
INSUFFICIENT_AFTER settle on credits, REJECT only when nothing was metered), O-3 (no MIN_CLUSTERS arm),
O-4 (REGRESSED with a >= $5 saving is VERIFIED, marked PERF_REGRESSED).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sqlglot = pytest.importorskip("sqlglot")
_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "snowflake" / "migrations"
_NAME = "V153__ledger_autobook_full_window_settle.sql"
_V153 = (_MIG / _NAME).read_text(encoding="utf-8")
_V145 = (_MIG / "V145__ledger_autobook_stamp_finding_type.sql").read_text(encoding="utf-8")
_V053 = (_MIG / "V053__action_layer_remediation_verify.sql").read_text(encoding="utf-8")
_V109 = (_MIG / "V109__warehouse_change_scan_fail_token.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str, name: str = "SP_LEDGER_AUTOBOOK") -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}(")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _cut(text: str, start: str, end: str) -> str:
    """Remove the ONE block that begins with `start` and ends with (and includes) `end`."""
    assert text.count(start) == 1, start[:60]
    i = text.index(start)
    j = text.index(end, i) + len(end)
    return text[:i] + text[j:]


def _once(text: str, new: str, old: str) -> str:
    assert text.count(new) == 1, new[:60]
    return text.replace(new, old)


def _ws(s: str) -> str:
    return " ".join(s.split())


_P153, _P145 = _proc(_V153), _proc(_V145)
_ADOPT_START = "    -- V153 ADOPT (Next-Fifty #5 follow-up)"
_ADOPT_END = "       AND l.SOURCE_CHANGE_ID IS NULL;\n\n"
_CLOSE_START = "    -- V153 close-out:"
_CLOSE_END = "       AND r.AFTER_CREDITS_PER_DAY IS NULL;\n\n"
_SETTLE_START = "    -- Settle forward-only."
_DIR_START = "            (r.SETTING = 'AUTO_SUSPEND'"
_DIR_END = "WHEN '3XLARGE' THEN 7 WHEN '4XLARGE' THEN 8 ELSE 0 END)\n"
_GATE = ("             WHERE r.VERDICT IN ('IMPROVED', 'NEUTRAL', 'REGRESSED', 'NO_BASELINE', 'INSUFFICIENT_AFTER')\n"
         "               AND CURRENT_DATE() > r.TRACKING_UNTIL\n"
         "               AND r.AFTER_CREDITS_PER_DAY IS NOT NULL\n")
_RN_RE = re.compile(r"ROW_NUMBER\(\) OVER \(\s*PARTITION BY r\.WAREHOUSE_NAME.*?ORDER BY r\.CHANGE_SEEN_AT, "
                    r"r\.CHANGE_ID\)", re.S)
_RATE = "COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)"
_VOL = "(r.AFTER_QUERIES / NULLIF(r.AFTER_DAYS, 0)) / NULLIF(r.BASELINE_QUERIES / 14.0, 0)"


def _adopt() -> str:
    return _P153.split(_ADOPT_START, 1)[1].split(_ADOPT_END, 1)[0]


def _settle() -> str:
    return _P153.split(_SETTLE_START, 1)[1].split(_CLOSE_START, 1)[0]


def _closeout() -> str:
    return _P153.split(_CLOSE_START, 1)[1]


def _dir_block(p: str) -> str:
    i = p.index(_DIR_START)
    return p[i:p.index(_DIR_END, i) + len(_DIR_END)]


# --------------------------------------------------------------------------------------------------
def test_v153_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v153.py")],
        env={**os.environ, "V153_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V153, (
        "V153 drifted from its forward-generation -- edit outputs/gen_v153.py, not the .sql, then regenerate.")


def test_v153_guarded_ordered_and_marked():
    assert "EXCEPTION (-20153, 'V153 requires V152 first - apply migrations in order.')" in _V153
    assert "IF (v < 152) THEN" in _V153 and "RAISE not_ready;" in _V153
    assert "SELECT 153 AS VERSION" in _V153 and "WHERE VERSION = 153)" in _V153
    assert ("-- >>> derived:SP_LEDGER_AUTOBOOK  (from V145; full-window settle gate, volume note, adopt, "
            "FLOAT rate, close-out; V153)") in _V153
    assert "-- >>> derived:SP_VERIFY_IDLE_SAVINGS  (from V053; skip rows tied to a detected change; V153)" in _V153
    assert "$_" not in _V153 and "RAISE EXCEPTION (" not in _V153
    # proc-only: two re-derivations, no schema / task / view / alert-config change
    assert _V153.count("CREATE OR REPLACE PROCEDURE") == 2
    for banned in ("CREATE OR REPLACE VIEW", "CREATE TASK", "ALTER TASK", "CREATE TABLE", "ALTER TABLE",
                   "ALERT_CONFIG", "ALERT_EVENTS"):
        assert banned not in _V153, banned


def test_v153_proc_is_v145_plus_only_the_locked_deltas():
    """Normalize-and-compare: strip every documented V153 delta -> the V145 proc, byte-for-byte.
    V145 = V118 (LBA-1 dedup) + the FINDING_TYPE stamp; V118 = the V038 core -- nothing in the lineage drops."""
    n = _P153
    n = _cut(n, _ADOPT_START, _ADOPT_END)                                   # D2
    n = _cut(n, _CLOSE_START, _CLOSE_END)                                   # D6
    n = _once(n, "    rate FLOAT;\n", "    rate NUMBER;\n")                  # D1
    n = _once(n, "COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD'",
              "COALESCE(TRY_TO_NUMBER(MAX(IFF(KEY = 'CREDIT_PRICE_USD'")
    n = _cut(n, "    -- V153: settle ONLY once", "Dollars are never adjusted for volume.\n")   # D5a
    n = _cut(n, "                   -- V153: per-day query volume", "AS VOL_RATIO,\n")      # D5c
    n = _once(n, _GATE, "             WHERE r.VERDICT <> 'PENDING'\n")                     # D4 (3 lines)
    n = _once(n, "' | measured on the full window: '", "' | measured '")                   # D5b
    n = _once(n, "|| ' credits/day (14d baseline) -> '", "|| ' -> '")
    n = _once(n, "|| 'd after (' || s.VERDICT", "|| 'd (' || s.VERDICT")
    n = _cut(n, "                        || ' | volume '", "latency/queue/failure axes were not judged.', '')\n")
    assert n == _P145, "V153 changed SP_LEDGER_AUTOBOOK beyond the documented deltas"


def test_lineage_features_survive():
    # V038 core
    assert "WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l\n" in _P153
    assert "999999999" in _P153 and "'AUTO:TASK_LEDGER_AUTOBOOK'" in _P153 and "* :rate * 30" in _P153
    assert "IFF(s.WH_SAVED_MONTHLY_USD >= 5, 'VERIFIED', 'REJECTED')" in _P153      # the $5 floor
    assert _P153.count("AND l.STATE = 'ESTIMATED'") == 2       # settle + close-out stay forward-only
    # V118 LBA-1
    assert "WHEN s.RN = 1 THEN ROUND(s.WH_SAVED_MONTHLY_USD, 2)" in _P153
    assert "LBA-1 co-attributed" in _P153 and "l2.SOURCE_CHANGE_ID = r.CHANGE_ID" in _P153
    # V145 lever stamp + the booking note D4 makes true
    assert ", SOURCE_CHANGE_ID, FINDING_TYPE)" in _P153
    assert "CASE WHEN r.SETTING = 'SIZE' THEN 'RESIZE' ELSE r.SETTING END" in _P153
    assert "the 14-day measured verdict settles it." in _P153


def test_settle_gate_is_the_closed_window_on_metered_credits():
    settle = _settle()
    assert _GATE in settle
    assert "<> 'PENDING'" not in _P153
    # the scan's own closed-window predicate (the same session-clock CURRENT_DATE() that wrote TRACKING_UNTIL)
    assert "WHERE CURRENT_DATE() > TRACKING_UNTIL AND VERDICT = 'PENDING'" in _V109
    assert "CURRENT_TIMESTAMP(), DATEADD('day', 14, CURRENT_DATE())" in _V109   # TRACKING_UNTIL = seen day + 14
    # V109 is still the scan's current definer (a later re-derive must re-check this gate's partner)
    later = [p.name for p in _MIG.glob("V*.sql") if int(p.name[1:4]) > 109
             and "PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_WAREHOUSE_CHANGE_SCAN(" in p.read_text(encoding="utf-8")]
    assert not later, later
    # STATE / VERIFIED_USD / VERIFIED_AT / VERIFIED_BY are the V145 assignments; volume never moves dollars
    head = settle.split("           NOTES = LEFT(COALESCE(l.NOTES, '') || ' | measured on", 1)[0]
    head = head.split("    UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER l\n", 1)[1]     # the SET list before NOTES
    assert head.startswith("       SET STATE = IFF(s.WH_SAVED_MONTHLY_USD >= 5, 'VERIFIED', 'REJECTED'),")
    assert "VOL_RATIO" not in head and "PERF_" not in head and "VERDICT" not in head


def test_settle_note_discloses_volume_and_perf_markers():
    settle = _settle()
    assert "(r.AFTER_QUERIES / NULLIF(r.AFTER_DAYS, 0))" in settle
    assert "/ NULLIF(r.BASELINE_QUERIES / 14.0, 0) AS VOL_RATIO" in settle
    assert "COALESCE(TO_VARCHAR(ROUND(s.VOL_RATIO, 2)), '?')" in settle       # NULL can't null the NOTE
    assert "IFF(s.VOL_RATIO < 0.7 OR s.VOL_RATIO > 1.3," in settle and "VOLUME_CONFOUNDED" in settle
    assert "IFF(s.VERDICT = 'REGRESSED' AND s.WH_SAVED_MONTHLY_USD >= 5," in settle
    assert "' | PERF_REGRESSED: " in settle
    assert "IFF(s.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER')," in settle
    assert "' | PERF_UNJUDGED: fewer than 20 queries in a window" in settle
    # the new markers sit BEFORE the unchanged V118 LBA-1 tail
    assert (settle.index("' | PERF_REGRESSED: ") < settle.index("' | PERF_UNJUDGED: ")
            < settle.index("IFF(s.WH_SAVED_MONTHLY_USD >= 5 AND s.RN > 1,"))


def test_closeout_rejects_only_when_nothing_was_metered():
    close = _closeout()
    assert "SET STATE = 'REJECTED'," in close
    assert "VERIFIED_USD" not in close                         # no dollars are ever booked by the close-out
    assert "VERIFIED_BY = 'AUTO:TASK_LEDGER_AUTOBOOK'" in close
    assert "AND r.VERDICT IN ('NO_BASELINE', 'INSUFFICIENT_AFTER')" in close
    assert "AND CURRENT_DATE() > r.TRACKING_UNTIL" in close
    assert "AND r.AFTER_CREDITS_PER_DAY IS NULL;" in close
    assert "no metered credits after the change, so no saving is booked." in close
    assert "AND l.STATE = 'ESTIMATED'" in close
    # settle (AFT NOT NULL) and close-out (AFT NULL) partition the closed rows: nothing is stranded
    assert "r.AFTER_CREDITS_PER_DAY IS NOT NULL" in _settle() and "IS NOT NULL" not in close
    assert _P153.index(_SETTLE_START) < _P153.index(_CLOSE_START) < _P153.index("    RETURN 'OK';")


def test_rate_is_float_not_scale0_number():
    assert "    rate FLOAT;" in _P153 and _RATE in _P153
    assert "TRY_TO_NUMBER(MAX(IFF(KEY = 'CREDIT_PRICE_USD'" not in _P153
    assert "rate NUMBER" not in _P153


def test_adopt_mirrors_the_app_twin_rule_and_precedes_the_insert():
    from app.config import LEDGER_AUTOBOOKED_LEVERS, LEDGER_TWIN_MATCH_DAYS
    from app.data import mart_sql
    adopt = _adopt()
    twin = mart_sql._ledger_twin_select()
    for line in ("ON UPPER(r.WAREHOUSE_NAME) = UPPER(TRIM(m.TARGET_OBJECT))",
                 "AND r.SETTING = IFF(UPPER(TRIM(m.FINDING_TYPE)) = 'RESIZE', 'SIZE', UPPER(TRIM(m.FINDING_TYPE)))",
                 "AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ >= DATEADD('hour', -1, m.CREATED_AT)",
                 f"AND r.CHANGE_SEEN_AT::TIMESTAMP_NTZ < DATEADD('day', {LEDGER_TWIN_MATCH_DAYS}, m.CREATED_AT)",
                 "WHERE m.SOURCE_CHANGE_ID IS NULL"):
        assert line in adopt and line in twin, line
    levers = ", ".join(f"'{x}'" for x in sorted(LEDGER_AUTOBOOKED_LEVERS))
    assert f"UPPER(TRIM(m.FINDING_TYPE)) IN ({levers})" in adopt and f"IN ({levers})" in twin
    assert "AND m.STATE = 'ESTIMATED'" in adopt
    assert "b.SOURCE_CHANGE_ID = r.CHANGE_ID" in adopt                 # never an already-booked change
    assert "PARTITION BY m.ITEM_ID ORDER BY r.CHANGE_SEEN_AT, r.CHANGE_ID) = 1" in adopt
    assert "PARTITION BY r.CHANGE_ID ORDER BY m.CREATED_AT, m.ITEM_ID) = 1" in adopt
    assert "SET SOURCE_CHANGE_ID = a.CHANGE_ID," in adopt and "adopted by the daily change scan" in adopt
    assert "STATE =" not in adopt.split("FROM (SELECT", 1)[0]          # adoption never settles
    assert _P153.index(_ADOPT_START) < _P153.index("    INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER")
    # the adopt's direction filter IS the V145 INSERT's, verbatim; the INSERT's is unchanged
    d145 = _dir_block(_P145)
    assert d145 in adopt
    assert _P153.split(_ADOPT_END, 1)[1].count(d145) == 1


def test_no_min_clusters_arm():
    # decision O-3: the lever stays registry-only (Next-Fifty #38 is wave 3). If it is ever taken it must
    # be a SEPARATE top-level INSERT, never an OR-nested correlated EXISTS inside the V145 direction block.
    assert "MIN_CLUSTERS" not in _P153
    assert _P153.count("INSERT INTO DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER") == 1


def test_verifier_skips_change_tied_rows_and_is_otherwise_v053():
    p = _proc(_V153, "SP_VERIFY_IDLE_SAVINGS")
    assert "          AND SOURCE_CHANGE_ID IS NULL\n    )," in p
    n = _cut(p, "          -- V153: a row tied to a detected change", "          AND SOURCE_CHANGE_ID IS NULL\n")
    assert n == _proc(_V053, "SP_VERIFY_IDLE_SAVINGS")


def test_tail_call_runs_after_both_procs_and_discloses_its_writes():
    call = "CALL DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK();"
    assert _V153.count(call) == 1
    i = _V153.index(call)
    assert _V153.index("PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_VERIFY_IDLE_SAVINGS()") < i
    assert i < _V153.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    comment = _V153[:i].rsplit("$$;", 1)[1]
    assert "ADOPT a manual ESTIMATED row booked in the last 3 days" in comment
    assert "ALTER SESSION SET TIMEZONE = 'America/Chicago'" in comment
    assert "ADOPT stamps may already be committed" in comment


def test_description_fits_and_is_quote_safe():
    m = re.search(r"SELECT 153 AS VERSION,\s*'((?:[^']|'')*)' AS DESCRIPTION", _V153, re.S)
    assert m, "SCHEMA_VERSION 153 description not found"
    assert len(m.group(1).replace("''", "'")) <= 4000
    assert "15-16 days" in m.group(1) and "PERF_UNJUDGED" in m.group(1)


# --- app mirrors of the proc (the Savings-ledger re-measure must agree with what the proc settles) ----
def test_app_remeasure_mirrors_the_proc_gate_rn_rate_and_volume():
    from app.data import mart_sql
    from app.data.common import account_today_sql
    sql = mart_sql.savings_ledger(limit=None)
    # the gate: same 5 verdicts + metered-after predicate; the app clock per the TZ standard
    assert "r.VERDICT IN ('IMPROVED', 'NEUTRAL', 'REGRESSED', 'NO_BASELINE', 'INSUFFICIENT_AFTER')" in sql
    assert f"AND {account_today_sql()} > r.TRACKING_UNTIL" in sql
    assert "AND r.AFTER_CREDITS_PER_DAY IS NOT NULL" in sql
    # the LBA-1 RN window is the proc's (compared whitespace-normalized: the layouts differ)
    proc_rn = {_ws(m) for m in _RN_RE.findall(_settle())}
    app_rn = {_ws(m) for m in _RN_RE.findall(sql)}
    assert len(proc_rn) == 1 and proc_rn == app_rn, (proc_rn, app_rn)
    # the rate + the volume ratio are textually the proc's
    assert _RATE in sql and _RATE in _P153
    assert _VOL in sql and _ws(_VOL) in _ws(_settle())
    # the $5 floor and the RN=1 / 0 split, like the proc's VERIFIED_USD CASE
    assert ">= 5," in sql.split("AS REMEASURED_14D_MONTHLY_USD", 1)[0].rsplit("AS MEASURED_AFTER_DAYS", 1)[1]


# --- lockstep (pinned to the WAVE TIP V154; these fail on the slice branch until the integrator lands
# --- validate.sql / DEPLOYMENT.md / README.md / admin._EXPECTED_MIGRATIONS for the whole wave) ----------
def test_validate_and_docs_track_v153():
    val = _read("snowflake/validate.sql")
    assert "V001..V159 applied" in val and "VERSION BETWEEN 1 AND 159) = 159" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert _NAME in _read(rel), rel


def test_v153_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 153 in _EXPECTED_MIGRATIONS
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in _EXPECTED_MIGRATIONS[153]
    assert not _EXPECTED_MIGRATIONS[153].rstrip().endswith(".")


def test_v153_plain_sql_parses():
    from tests.test_migrations_parse import _plain_statements
    statements = list(_plain_statements(_V153))
    assert any(s.startswith("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION") for s in statements)
    for statement in statements:
        sqlglot.parse(statement, dialect="snowflake")


def test_v153_proc_dml_parses():
    """sqlglot can't read Scripting, but each DML statement inside the body is plain SQL once the
    :rate bind is replaced -- parse ADOPT (double QUALIFY in UPDATE ... FROM), INSERT, settle, close-out."""
    body = _P153.split("BEGIN\n", 1)[1].rsplit("    RETURN 'OK';", 1)[0].replace(":rate", "3.68")
    stmts = [s.strip() for s in body.split(";\n") if s.strip()]
    dml = [s for s in stmts if re.sub(r"^(\s*--[^\n]*\n)*", "", s).lstrip().startswith(("UPDATE", "INSERT"))]
    assert len(dml) == 4, len(dml)
    for s in dml:
        sqlglot.parse_one(s, read="snowflake")
