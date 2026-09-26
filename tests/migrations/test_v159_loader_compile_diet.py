"""V159 locks: the loader compile diet (wave-2b rework, owner decisions D5 + D6).

Two procs recompiled a heavy ACCOUNT_USAGE statement every hour for data that changes far less often.
V159 makes them skip the work instead, with two INSERTION-ONLY re-derivations from their current definers:
  * D5  SP_LOAD_MARTS_V27(VARCHAR, FLOAT) from V152 -- the Central hour is read ONCE at the top of the HOURLY
        branch and the day-grain arms [1] MART_WAREHOUSE_EFFICIENCY_DAILY, [6] MART_TASK_GRAPH_DAILY and [6b]
        MART_TASK_NODE_DAILY are each wrapped in IF (d > 2 OR MOD(ct_hour, 4) = 0): the 00/04/08/12/16/20
        Central cycles, and ALWAYS on the d > 2 reconcile / backfill path.
  * D6  SP_CHANGE_ATTRIBUTION() from V033 -- an early-return guard runs the unchanged UPDATE only while a
        registry row seen in the last 3 hours is still unattributed.
Byte-locked to outputs/gen_v159.py; removing the declared insertions gives back each base byte-for-byte
(the round-13 class). The gate hours, the reconcile escape and the attribution attempt count are proven
by small models over a real year of Central task fires (zoneinfo) and the real SQL gate expression
(evaluated by sqlite3), not by re-reading the generator.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIGDIR = _ROOT / "snowflake" / "migrations"
_NAME = "V159__loader_compile_diet.sql"
_MIG = (_MIGDIR / _NAME).read_text(encoding="utf-8")
_V152 = (_MIGDIR / "V152__pipeline_freshness_coverage.sql").read_text(encoding="utf-8")
_V033 = (_MIGDIR / "V033__change_attribution.sql").read_text(encoding="utf-8")
_CT = ZoneInfo("America/Chicago")
_GATED = {"[1]": ("MART_WAREHOUSE_EFFICIENCY_DAILY", "wh_eff"),
          "[6]": ("MART_TASK_GRAPH_DAILY", "graphs"),
          "[6b]": ("MART_TASK_NODE_DAILY", "task_node")}
_DAILY_SPLIT = "    IF (UPPER(:SCOPE) = 'DAILY') THEN\n"

# ---------------------------------------------------------------------------------------------------
# Test-side copies of every insertion (independent of outputs/gen_v159.py).
# ---------------------------------------------------------------------------------------------------
_DECL = "    ct_hour INT;              -- V159 (D5): Central hour of this run, read once (the 4-hour gate)\n"
_HOUR_READ = "        SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())) INTO :ct_hour;\n"
_HOUR_BLOCK = (
    "        -- V159 compile diet (D5): the three DAY-grain arms whose ACCOUNT_USAGE MERGEs dominate this\n"
    "        -- loader's compile -- [1] MART_WAREHOUSE_EFFICIENCY_DAILY, [6] MART_TASK_GRAPH_DAILY and [6b]\n"
    "        -- MART_TASK_NODE_DAILY -- run every 4th Central hour (00, 04, 08, 12, 16, 20) instead of every\n"
    "        -- hour, and ALWAYS when d > 2: SP_NIGHTLY_RECONCILE DELETEs D-3..today of the first two and\n"
    "        -- re-loads them with ('HOURLY', 3), and backfills pass 90/365. The hourly task passes 2. A gated-off\n"
    "        -- arm is not a failure (req_fail / opt_fail untouched) and appends no :loaded token, so its\n"
    "        -- SOURCE_FRESHNESS_STATE row keeps its last stamp -- every name here contains DAILY, so the shared\n"
    "        -- 30h cadence rule never reads it stale. Every other arm below still runs every hour.\n"
    + _HOUR_READ + "\n"
)
_ATTR_GUARD = (
    "    -- V159 compile diet (D6): the UPDATE below compiles an 8-day ACCOUNT_USAGE.QUERY_HISTORY join, and it\n"
    "    -- ran every hour. Registry rows only arrive when SP_WAREHOUSE_CHANGE_SCAN runs (the 06:40 daily scan,\n"
    "    -- or an on-demand Run scan), and each row's evidence window is fixed around its CHANGE_SEEN_AT\n"
    "    -- (-65/+5 min), so once QUERY_HISTORY has caught up (it lags ~45 min) a later retry finds nothing\n"
    "    -- new. Run the pass only while a row seen in the last 3 hours is still unattributed: ~3 hourly\n"
    "    -- attempts per change, the first at ~07:07 after the 06:40 scan as before; every other hour costs\n"
    "    -- one small-table probe.\n"
    "    -- CHANGE_SEEN_AT is the scan's TIMESTAMP_LTZ CURRENT_TIMESTAMP() stamp (V024/V109), compared on the\n"
    "    -- same clock as the 7-day filter below. When the pass runs, the UPDATE is unchanged: it still retries\n"
    "    -- every unattributed row of the last 7 days.\n"
    "    IF (NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY\n"
    "                    WHERE CHANGED_BY IS NULL\n"
    "                      AND CHANGE_SEEN_AT >= DATEADD('hour', -3, CURRENT_TIMESTAMP()))) THEN\n"
    "        RETURN 'attribution pass skipped (no unattributed change seen in the last 3h)';\n"
    "    END IF;\n"
    "\n"
)


def _open(label: str) -> str:
    return (f"        IF (d > 2 OR MOD(ct_hour, 4) = 0) THEN   -- V159 (D5) gate {label}: every 4th Central hour; "
            "always when d > 2\n")


def _close(label: str) -> str:
    return f"        END IF;   -- V159 (D5) gate {label}\n"


_MARKERS = {
    "SP_LOAD_MARTS_V27": ("-- >>> derived:SP_LOAD_MARTS_V27  (from V152;",
                          "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27"
                          "(SCOPE VARCHAR, DAYS_BACK FLOAT)"),
    "SP_CHANGE_ATTRIBUTION": ("-- >>> derived:SP_CHANGE_ATTRIBUTION  (from V033;",
                              "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_CHANGE_ATTRIBUTION()"),
}


# ---------------------------------------------------------------------------------------------------
# Helpers (read the module globals at call time, so a mutation harness can swap a temp copy in).
# ---------------------------------------------------------------------------------------------------
def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _one(pattern: str, text: str) -> str:
    found = re.findall(pattern, text, re.S)
    assert len(found) == 1, f"expected exactly one match for {pattern[:60]!r}, got {len(found)}"
    return found[0]


def _proc(text: str, name: str) -> str:
    return _one(rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n", text)


def _marts() -> str:
    return _proc(_MIG, "SP_LOAD_MARTS_V27")


def _attr() -> str:
    return _proc(_MIG, "SP_CHANGE_ATTRIBUTION")


def _hourly() -> str:
    return _marts().split(_DAILY_SPLIT, 1)[0]


_GATE_LINE_RE = re.compile(r"^        IF \((?P<cond>.+?)\) THEN   -- V159 \(D5\) gate (?P<label>\[\w+\]):.*$", re.M)


def _gates() -> dict[str, str]:
    """{label: SQL condition} for every V159 gate line in the loader."""
    found = {m.group("label"): m.group("cond") for m in _GATE_LINE_RE.finditer(_marts())}
    assert len(found) == len(_GATE_LINE_RE.findall(_marts())), "a gate label is used twice"
    return found


def _sql_predicate(cond: str):
    """Evaluate the REAL Snowflake Scripting condition text with sqlite3 (MOD registered): scripting
    variables d / ct_hour become named binds, so a mutated operator or constant changes the answer."""
    con = sqlite3.connect(":memory:")
    con.create_function("MOD", 2, lambda a, b: a % b, deterministic=True)
    sql = "SELECT (" + re.sub(r"\b(d|ct_hour)\b", r":\1", cond) + ")"
    return lambda d, hour: bool(con.execute(sql, {"d": d, "ct_hour": hour}).fetchone()[0])


def _clamp_d():
    """The proc's own DAYS_BACK -> d clamp, evaluated from its text with sqlite3."""
    expr = _one(r"\n    d := (GREATEST\(.*?\))::INT;\n", _marts())
    sql = ("SELECT CAST(" + expr.replace("GREATEST(", "MAX(").replace("LEAST(", "MIN(")
           .replace("DAYS_BACK", ":days_back") + " AS INTEGER)")
    con = sqlite3.connect(":memory:")
    return lambda days_back: con.execute(sql, {"days_back": days_back}).fetchone()[0]


def _central_hour(instant: datetime) -> int:
    """HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())) for a UTC instant."""
    return instant.astimezone(_CT).hour


def _hourly_fires(year: int) -> list[datetime]:
    """Every TASK_LOAD_HOURLY fire of a year as UTC instants, from its real cron (minute + zone)."""
    cron = _one(r"TASK_LOAD_HOURLY\n.*?SCHEDULE = 'USING CRON (\d+) \* \* \* \* ([\w/]+)'",
                _read("snowflake/migrations/V002__facts.sql"))
    minute, zone = int(cron[0]), cron[1]
    assert zone == "America/Chicago"
    start = datetime(year, 1, 1, tzinfo=_CT).astimezone(UTC).replace(minute=0, second=0)
    end = datetime(year + 1, 1, 1, tzinfo=_CT).astimezone(UTC)
    fires, t = [], start
    while t < end:
        cand = t.replace(minute=minute)
        if cand.astimezone(ZoneInfo(zone)).minute == minute:     # Central offsets are whole hours
            fires.append(cand)
        t += timedelta(hours=1)
    return fires


# ---------------------------------------------------------------------------------------------------
# Generation, guard, inventory, lineage
# ---------------------------------------------------------------------------------------------------
def test_v159_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v159.py")],
        env={**os.environ, "V159_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _MIG, (
        "V159 drifted from its forward-generation -- edit outputs/gen_v159.py, not the .sql, then regenerate.")


def test_v159_guarded_and_versioned():
    assert "EXCEPTION (-20159, 'V159 requires V158 first - apply migrations in order.')" in _MIG
    assert "IF (v < 158) THEN" in _MIG
    assert _MIG.index("EXECUTE IMMEDIATE") < _MIG.index("CREATE OR REPLACE")   # guard runs first
    assert "SELECT 159 AS VERSION" in _MIG and "WHERE VERSION = 159)" in _MIG
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 159);")


def test_v159_object_inventory_is_two_procs_only():
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 2
    outside = _MIG.replace(_marts(), "").replace(_attr(), "")
    for banned in ("CREATE TASK", "ALTER TASK", "EXECUTE TASK", "ALERT_CONFIG", "CREATE TABLE", "ALTER TABLE",
                   "CREATE OR REPLACE VIEW", "CREATE OR REPLACE FUNCTION", "SYSTEM$TASK_DEPENDENTS_ENABLE",
                   "GRANT ", "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):
        assert banned not in outside, banned
    assert not re.search(r"^\s*CALL\b", outside, re.M), "V159 must not CALL anything at apply time"


def test_v159_markers_sit_directly_above_each_create():
    for name, (marker, create) in _MARKERS.items():
        assert _MIG.count(marker) == 1, name
        line = next(ln for ln in _MIG.splitlines() if ln.startswith(marker))
        assert line.endswith(", V159)"), line
        assert f"{line}\n{create}" in _MIG, f"{name}: marker must sit directly above its CREATE"


def test_v159_bases_are_the_immediately_previous_definers():
    """The round-13 class: each base must be the CURRENT definer. SP_CHANGE_ATTRIBUTION has had exactly one
    definer (V033) in the whole migration set -- grep every migration, not just the named base."""
    from tests.test_proc_lineage import _definers, _migrations
    defs = _definers(_migrations())
    assert max(v for v in defs["SP_LOAD_MARTS_V27"] if v < 159) == 152
    assert [v for v in defs["SP_CHANGE_ATTRIBUTION"] if v < 159] == [33]
    assert 159 in defs["SP_LOAD_MARTS_V27"] and 159 in defs["SP_CHANGE_ATTRIBUTION"]


# ---------------------------------------------------------------------------------------------------
# Normalize-and-compare: nothing outside the declared insertions moved
# ---------------------------------------------------------------------------------------------------
def test_v159_marts_loader_is_v152_plus_only_the_declared_insertions():
    p159 = _marts()
    for piece in (_DECL, _HOUR_BLOCK):
        assert p159.count(piece) == 1, piece[:60]
    for label in _GATED:
        assert p159.count(_open(label)) == 1 and p159.count(_close(label)) == 1, label
        assert p159.count(_open(label) + "        BEGIN\n") == 1 and p159.count("        END;\n" + _close(label)) == 1
    # positional: each insertion sits at its declared anchor, not merely somewhere in the body
    assert "    d INT;\n" + _DECL in p159
    assert "    IF (UPPER(:SCOPE) = 'HOURLY') THEN\n\n" + _HOUR_BLOCK in p159
    stripped = p159.replace(_DECL, "").replace(_HOUR_BLOCK, "")
    for label in _GATED:
        stripped = stripped.replace(_open(label), "").replace(_close(label), "")
    assert stripped == _proc(_V152, "SP_LOAD_MARTS_V27"), (
        "V159 changed SP_LOAD_MARTS_V27 beyond the ct_hour declaration, the hour read and the three gates")
    assert p159.count("ct_hour") == 1 + 1 + 3                    # declared, read once, used by 3 gates


def test_v159_attribution_is_v033_plus_only_the_guard():
    a159 = _attr()
    assert a159.count(_ATTR_GUARD) == 1
    # positional: the guard is the FIRST thing the proc does, before V033's comment and UPDATE
    assert "AS\n$$\nBEGIN\n" + _ATTR_GUARD + "    -- Attribute unattributed registry rows" in a159
    assert a159.replace(_ATTR_GUARD, "") == _proc(_V033, "SP_CHANGE_ATTRIBUTION"), (
        "V159 changed SP_CHANGE_ATTRIBUTION beyond the recent-unattributed guard")


# ---------------------------------------------------------------------------------------------------
# D5 gate structure
# ---------------------------------------------------------------------------------------------------
def test_v159_each_gate_wraps_exactly_one_day_grain_arm():
    hourly = _hourly()
    daily = _marts().split(_DAILY_SPLIT, 1)[1]
    assert set(_gates()) == set(_GATED), "exactly arms [1], [6] and [6b] are gated"
    assert "ct_hour" not in daily and "V159" not in daily        # the DAILY scope is untouched
    for label, (table, token) in _GATED.items():
        start = hourly.index(_open(label))
        end = hourly.index(_close(label))
        span = hourly[start + len(_open(label)):end]
        # the gate opens directly on the arm's BEGIN and closes directly after its END; (the whole block, so a
        # gated-off hour can neither run the MERGE nor reach the token append or the failure counters)
        assert span.startswith("        BEGIN\n") and span.endswith("        END;\n"), label
        assert re.findall(r"MERGE INTO DBA_MAINT_DB\.OVERWATCH\.(\w+) t", span) == [table], label
        assert span.count(f"loaded := loaded || '{token} ';") == 1, label
        assert "ELSE" not in span.replace("ELSE 0", "")          # no else-branch: a skip does nothing at all
    # every other HOURLY arm, the hour-independent reads and the freshness stamp stay ungated
    spans = [(hourly.index(_open(lb)), hourly.index(_close(lb))) for lb in _GATED]
    for token in ("qfam", "role_hr", "schema_hr", "tagcov", "alloc", "alloc_xdim", "timeline"):
        pos = hourly.index(f"loaded := loaded || '{token} ';")
        assert not any(s < pos < e for s, e in spans), f"{token} must still load every hour"
    stamp = hourly.index("MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t")
    assert all(e < stamp for _, e in spans)


def test_v159_central_hour_is_read_once_before_any_gate():
    body = _marts()
    hourly = _hourly()
    assert body.count(_HOUR_READ) == 1 and hourly.count(_HOUR_READ) == 1
    assert body.count("INTO :ct_hour") == 1 and body.count("ct_hour :=") == 0
    read = hourly.index(_HOUR_READ)
    assert hourly.index("    IF (UPPER(:SCOPE) = 'HOURLY') THEN\n") < read
    assert read < min(hourly.index(_open(lb)) for lb in _GATED)
    assert body.index("d := GREATEST(") < read                   # d is set before the gates read it
    # the TIMEZONE STANDARD: the hour is the Central wall-clock hour, never the session's
    assert "HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP()))" in _HOUR_READ
    assert not re.search(r"HOUR\(CURRENT_TIMESTAMP", body) and "DATE_PART('hour'" not in body


def test_v159_gate_selects_exactly_the_intended_central_hours():
    """Model the REAL condition text: d <= 2 opens only 00/04/08/12/16/20; d > 2 opens every hour."""
    gates = _gates()
    assert len(set(gates.values())) == 1, f"the three gates must share one condition: {gates}"
    pred = _sql_predicate(next(iter(gates.values())))
    for d in (1, 2):
        assert {h for h in range(24) if pred(d, h)} == {0, 4, 8, 12, 16, 20}, d
    for d in (3, 7, 90, 365, 400):
        assert {h for h in range(24) if pred(d, h)} == set(range(24)), d


def test_v159_reconcile_and_backfill_always_run_the_gated_arms():
    """The :d > 2 escape is mandatory: SP_NIGHTLY_RECONCILE DELETEs D-3..today of two of the gated marts and
    re-loads with ('HOURLY', 3); the hourly task passes 2. Read every caller's real argument and push it
    through the proc's own DAYS_BACK clamp and the real gate condition."""
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    pred = _sql_predicate(next(iter(_gates().values())))
    clamp = _clamp_d()
    recon = _latest_proc_bodies()["SP_NIGHTLY_RECONCILE"]
    recon_call = recon.index("CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);")
    for table in ("MART_WAREHOUSE_EFFICIENCY_DAILY", "MART_TASK_GRAPH_DAILY"):
        delete = re.search(rf"DELETE FROM DBA_MAINT_DB\.OVERWATCH\.{table}\n\s+WHERE DAY >= DATEADD\('day', -3, "
                           r"CURRENT_DATE\(\)\);", recon)
        assert delete and delete.start() < recon_call, table
    task = _one(r"CREATE OR REPLACE TASK DBA_MAINT_DB\.OVERWATCH\.TASK_LOAD_MARTS_V27_HOURLY\n.*?"
                r"SP_LOAD_MARTS_V27\('HOURLY', (\d+)\);", _read("snowflake/migrations/V041__loader_efficiency.sql"))
    later = [p.name for p in _MIGDIR.glob("V*.sql")
             if int(re.match(r"V(\d+)", p.name).group(1)) > 41
             and re.search(r"(?:CREATE|ALTER)\s+(?:OR REPLACE\s+)?TASK\s+(?:IF (?:NOT )?EXISTS\s+)?"
                           r"DBA_MAINT_DB\.OVERWATCH\.TASK_LOAD_MARTS_V27_HOURLY\b(?!\s+(?:SUSPEND|RESUME)\b)",
                           p.read_text(encoding="utf-8"))]
    assert not later, f"TASK_LOAD_MARTS_V27_HOURLY was re-created later: {later}"
    backfills = re.findall(r"^CALL DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_MARTS_V27\('HOURLY', (\d+)\);",
                           _read("snowflake/backfill_365.sql"), re.M)          # live CALLs, not commented ones
    assert len(backfills) == 1
    backfill = int(backfills[0])
    assert clamp(int(task)) == 2 and clamp(None) == 2
    for hour in range(24):
        assert pred(clamp(3), hour), f"the reconcile must run the gated arms at Central hour {hour}"
        assert pred(clamp(backfill), hour)
    assert sum(pred(clamp(int(task)), h) for h in range(24)) == 6


def test_v159_gate_over_a_real_year_of_hourly_task_fires():
    """Six gated cycles on EVERY Central day of 2026 (DST days included), a 4h spacing in real time --
    5h across the November fall-back night and 3h across the March spring-forward, as the header discloses."""
    pred = _sql_predicate(next(iter(_gates().values())))
    fires = _hourly_fires(2026)
    on = [f for f in fires if pred(2, _central_hour(f))]
    per_day = Counter(f.astimezone(_CT).date() for f in on)
    assert len(per_day) == 365 and set(per_day.values()) == {6}
    gaps = Counter(b - a for a, b in pairwise(on))
    assert gaps[timedelta(hours=5)] == 1 and gaps[timedelta(hours=3)] == 1
    assert set(gaps) == {timedelta(hours=3), timedelta(hours=4), timedelta(hours=5)}
    long_gap = next(b for a, b in pairwise(on) if b - a == timedelta(hours=5))
    assert long_gap.astimezone(_CT).date().isoformat() == "2026-11-01"      # fall-back Sunday
    # weekly run count behind the saving estimate: 6 cycles x 7 + the reconcile's 7 = 49 (was 24 x 7 + 7)
    assert 6 * 7 + 7 == 49 and len(fires) // 365 == 24


def test_v159_skipped_arm_counts_as_ok_and_leaves_freshness_alone():
    """A gated-off hour: the arm's MERGE, token append and failure counter are all inside the gate, so the
    run's :loaded omits the token, the token-gated freshness MERGE does not stamp that source, and the
    verdict is untouched. The three names contain DAILY, so the shared 30h rule never reads them stale
    at a 4h (5h) refresh."""
    from app.config import THRESHOLDS
    hourly = _hourly()
    srcmap = {tok: name for name, tok in re.findall(
        r"\('(\w+)', '(\w+)'\)", _one(r"FROM VALUES\s*\n(.*?)AS srcmap\(SOURCE_NAME, TOKEN\)", hourly))}
    for label, (table, token) in _GATED.items():
        span = hourly[hourly.index(_open(label)):hourly.index(_close(label))]
        assert "_fail := " in span and srcmap[token] == table
        outside = hourly.replace(span, "")
        assert f"'{token} '" not in outside and f"'{table} - other marts unaffected'" not in outside
        assert "DAILY" in table
    assert "WHERE ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' '))" in hourly
    assert THRESHOLDS["stale_daily_fact_hours"] >= 5 * 2
    # the verdict RETURN is byte-identical (no skip note that the reconcile's FAIL/ERRORS scan could misread)
    assert _marts().endswith(_proc(_V152, "SP_LOAD_MARTS_V27")[-450:])


# ---------------------------------------------------------------------------------------------------
# D6 attribution gate
# ---------------------------------------------------------------------------------------------------
def test_v159_attribution_probe_matches_the_registry_clock():
    """CHANGE_SEEN_AT is TIMESTAMP_LTZ stamped with CURRENT_TIMESTAMP() by the change scan, so the probe
    compares instants on the same CURRENT_TIMESTAMP() clock as the proc's own 7-day filter -- no zone."""
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    ddl = _read("snowflake/migrations/V024__warehouse_change_scorecard.sql")
    assert re.search(r"CREATE TABLE IF NOT EXISTS DBA_MAINT_DB\.OVERWATCH\.WAREHOUSE_CHANGE_REGISTRY \(.*?"
                     r"CHANGE_SEEN_AT\s+TIMESTAMP_LTZ NOT NULL", ddl, re.S)
    scan = _latest_proc_bodies()["SP_WAREHOUSE_CHANGE_SCAN"]
    ins = scan[scan.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY"):]
    assert re.match(r"INSERT INTO [^\n]*\n\s+\(WAREHOUSE_NAME, COMPANY, SETTING, OLD_VALUE, NEW_VALUE, "
                    r"CHANGE_SEEN_AT, TRACKING_UNTIL\)\n\s+SELECT d\.WAREHOUSE_NAME, d\.COMPANY, d\.SETTING, "
                    r"d\.OLD_VALUE, d\.NEW_VALUE,\n\s+CURRENT_TIMESTAMP\(\),", ins)
    a = _attr()
    guard = a[a.index("    IF (NOT EXISTS"):a.index("    END IF;\n") + len("    END IF;\n")]
    assert "FROM DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY" in guard
    assert "WHERE CHANGED_BY IS NULL" in guard
    assert "AND CHANGE_SEEN_AT >= DATEADD('hour', -3, CURRENT_TIMESTAMP())" in guard
    assert "AND r.CHANGE_SEEN_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())" in a    # the same clock
    assert "CONVERT_TIMEZONE" not in a and "TIMESTAMP_NTZ" not in a
    # early return BEFORE the (unchanged) UPDATE; the pass still RETURNs its old string when it runs
    assert a.index(guard) < a.index("UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t")
    assert "RETURN 'attribution pass skipped" in guard
    assert re.findall(r"RETURN '([^']*)'", a) == [
        "attribution pass skipped (no unattributed change seen in the last 3h)", "attribution pass complete"]


def _attr_window_hours() -> int:
    guard = _attr()[_attr().index("    IF (NOT EXISTS"):]
    return -int(_one(r"CHANGE_SEEN_AT >= DATEADD\('hour', (-?\d+), CURRENT_TIMESTAMP\(\)\)", guard))


def test_v159_attribution_attempts_per_change_model():
    """Each change gets ~3 attempts (fires with seen <= fire <= seen + window): the 06:40 daily scan's rows
    are first tried at 07:07 exactly as before (challenge #3), on every day of the year, and an on-demand
    scan at any minute still gets 3 attempts, the first within the hour."""
    window = timedelta(hours=_attr_window_hours())
    assert window == timedelta(hours=3)
    fires = _hourly_fires(2026)

    def attempts(seen: datetime) -> list[datetime]:
        return [f for f in fires if seen <= f <= seen + window]

    day = datetime(2026, 1, 1, tzinfo=_CT)
    while day.year == 2026:
        seen = day.replace(hour=6, minute=40, second=30).astimezone(UTC)
        tried = attempts(seen)
        assert len(tried) == 3, day.date()
        assert tried[0].astimezone(_CT).strftime("%H:%M") == "07:07", day.date()
        day += timedelta(days=1)
    for minute in range(60):
        seen = datetime(2026, 6, 10, 14, minute, 30, tzinfo=_CT).astimezone(UTC)
        tried = attempts(seen)
        assert len(tried) == 3 and tried[0] - seen < timedelta(hours=1), minute
        # the last attempt comes >= 2h after the scan: QUERY_HISTORY (~45 min lag) has caught up by then
        assert tried[-1] - seen >= timedelta(hours=2)


# ---------------------------------------------------------------------------------------------------
# Disclosures, docs, app captions
# ---------------------------------------------------------------------------------------------------
def test_v159_header_discloses_every_trade():
    head = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for phrase in ("up to 4h old", "5h across the November DST fall-back night", "SP_SLO_BREACH_SCAN",
                   "SLO_OBJECTIVES", "is empty today", "refills today at the next 4-hour slot",
                   "pass 3 to force", "not retried unless a newer unattributed change",
                   "Pre-existing and NOT changed here", "ESTIMATES", "175 to 49 runs/week",
                   "D5", "D6", "no tail CALL"):
        assert phrase in head, phrase
    # every latest proc that READS one of the gated marts is named in the latency disclosure
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    rx = re.compile(r"(?:(?<!DELETE )FROM|JOIN)\s+DBA_MAINT_DB\.OVERWATCH\.("
                    + "|".join(t for t, _ in _GATED.values()) + r")\b")
    readers = {n for n, b in _latest_proc_bodies().items() if rx.search(b) and n != "SP_LOAD_MARTS_V27"}
    assert readers >= {"SP_SLO_BREACH_SCAN"}
    for name in readers:
        assert name in head, f"{name} reads a mart V159 slows to 4h but the header does not disclose it"


def test_v159_app_captions_no_longer_claim_hourly():
    """The captions under every panel fed by the three gated marts say 4h, never hourly."""
    names = "|".join(t for t, _ in _GATED.values())
    src_re = re.compile(r"(?:mart_source|source)=f?\"((?:" + names + r")[^\"]*)\"")
    labels: dict[str, list[str]] = {}
    for path in sorted((_ROOT / "app" / "ui").rglob("*.py")):
        for label in src_re.findall(path.read_text(encoding="utf-8")):
            labels.setdefault(path.relative_to(_ROOT).as_posix(), []).append(label)
    for rel, found in labels.items():
        for label in found:
            assert "hourly" not in label.lower(), f"{rel}: {label}"
    expected = {"app/ui/pages/cost_parts/optimize.py": 3, "app/ui/pages/cost_parts/unit_costs.py": 1,
                "app/ui/pages/operations.py": 1}
    for rel, n in expected.items():
        assert sum("refreshed every 4h" in lb for lb in labels.get(rel, [])) == n, rel
    ops = _read("app/ui/pages/operations.py")
    empty = ops[ops.index("No per-node timing yet"):ops.index("once V058 is applied")]
    assert "hourly" not in empty and "every 4 hours" in empty


def test_v159_operator_docs_carry_the_new_cadence():
    runbook = _read("RUNBOOK.md")
    assert "**Loader compile diet (V159):**" in runbook
    for phrase in ("00/04/08/12/16/20 Central", "CALL SP_LOAD_MARTS_V27('HOURLY', 3)", "attribution pass skipped",
                   "Since V159 the hourly attribution pass"):
        assert phrase in runbook, phrase
    chain = _read("snowflake/loader_chain_check.sql")
    assert "--     CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);   -- 3, not 2: since V159" in chain
    assert "SP_LOAD_MARTS_V27('HOURLY', 2)" not in chain


def test_v159_description_is_escaped_and_fits():
    m = re.search(r"SELECT 159 AS VERSION,\s*'((?:[^']|'')*)'\s*AS DESCRIPTION", _MIG, re.S)
    assert m, "SCHEMA_VERSION insert must use the SELECT ... AS VERSION, '...' AS DESCRIPTION idiom"
    assert len(m.group(1).replace("''", "'")) <= 4000


def test_v159_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    statements = list(_plain_statements(_MIG))
    assert any("SELECT 159 AS VERSION" in s for s in statements)
    for statement in statements:
        sqlglot.parse(statement, dialect="snowflake")


# ---------------------------------------------------------------------------------------------------
# Lockstep with the shared files. Pinned to the WAVE-2b TIP (V159): EXPECTED to fail on the w2br-v159 slice
# branch until the integrator lands validate.sql / DEPLOYMENT.md / README.md / admin _EXPECTED_MIGRATIONS.
# ---------------------------------------------------------------------------------------------------
def test_validate_and_docs_track_v159():
    val = _read("snowflake/validate.sql")
    assert "V001..V159 applied" in val and "VERSION BETWEEN 1 AND 159) = 159" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert _NAME in _read(rel), rel


def test_v159_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 159 in _EXPECTED_MIGRATIONS
