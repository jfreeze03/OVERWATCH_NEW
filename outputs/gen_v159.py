#!/usr/bin/env python3
"""Forward-generate V159: the loader compile diet (wave-2b rework, owner decisions D5 + D6).

DIAG_CS_SELF_COST (2026-09-26) put OVERWATCH's ten heaviest scheduled compile families at ~166 min/week on WH_ALFA_ADMIN (a floor, not the whole scheduled total).
After the alert scan, the next-largest families are two procs that recompile a heavy ACCOUNT_USAGE statement
every hour for data that changes far less often:

  * SP_LOAD_MARTS_V27 HOURLY arms [1] MART_WAREHOUSE_EFFICIENCY_DAILY (175 runs/wk x 9.2 s = 27.0 min) and
    [6] MART_TASK_GRAPH_DAILY (175 x 5.1 s = 14.8 min); [6b] MART_TASK_NODE_DAILY sits below the panel's cut.
  * SP_CHANGE_ATTRIBUTION's UPDATE WAREHOUSE_CHANGE_REGISTRY (168 x 5.1 s = 14.4 min).

Two INSERTION-ONLY re-derivations, each from its CURRENT definer (tests/test_proc_lineage.py):
  D5  SP_LOAD_MARTS_V27(VARCHAR, FLOAT) from V152 -- read the Central hour ONCE at the top of the HOURLY
      branch and wrap arms [1], [6] and [6b] each in IF (d > 2 OR MOD(ct_hour, 4) = 0): every 4th Central
      hour, and ALWAYS on the d > 2 reconcile / backfill path (SP_NIGHTLY_RECONCILE DELETEs D-3..today and
      re-loads with ('HOURLY', 3), V064). The arms' own text is untouched (not even re-indented).
  D6  SP_CHANGE_ATTRIBUTION() from V033 (its only definer) -- an early-return guard: run the UPDATE only
      IF EXISTS a registry row with CHANGED_BY IS NULL and CHANGE_SEEN_AT within the last 3 hours
      (CHANGE_SEEN_AT is the scan's TIMESTAMP_LTZ CURRENT_TIMESTAMP() stamp, V024/V109).
Removing the inserted text gives back each base byte-for-byte (tests/migrations/test_v159_loader_compile_diet.py).
No task / schedule / rule / table change and no tail CALL.

Run: python outputs/gen_v159.py   (V159_OUT overrides the output path; the byte-identity test sets it)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE_MARTS = MIG / "V152__pipeline_freshness_coverage.sql"
BASE_ATTR = MIG / "V033__change_attribution.sql"


def _one(pattern: str, text: str, what: str) -> str:
    matches = re.findall(pattern, text, re.S)
    assert len(matches) == 1, f"{what}: expected exactly 1 definition, got {len(matches)}"
    return matches[0]


def _insert_after(text: str, anchor: str, new: str, what: str) -> str:
    assert text.count(anchor) == 1, f"{what}: anchor must occur exactly once, got {text.count(anchor)}"
    out = text.replace(anchor, anchor + new, 1)
    assert out.count(anchor + new) == 1, f"{what}: insertion must land exactly once"
    return out


# ---------------------------------------------------------------------------------------------------
# D5  SP_LOAD_MARTS_V27(VARCHAR, FLOAT)  (base V152)
# ---------------------------------------------------------------------------------------------------
marts_base = _one(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_MARTS_V27\(.*?\$\$;\n",
                  BASE_MARTS.read_text(encoding="utf-8"), "SP_LOAD_MARTS_V27 (V152)")
assert marts_base.startswith(
    "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27(SCOPE VARCHAR, DAYS_BACK FLOAT)")
assert "ct_hour" not in marts_base

GATE = "IF (d > 2 OR MOD(ct_hour, 4) = 0) THEN"

M_DECL_ANCHOR = "    d INT;\n"
M_DECL = "    ct_hour INT;              -- V159 (D5): Central hour of this run, read once (the 4-hour gate)\n"

M_HOUR_ANCHOR = "    IF (UPPER(:SCOPE) = 'HOURLY') THEN\n\n"
M_HOUR = (
    "        -- V159 compile diet (D5): the three DAY-grain arms whose ACCOUNT_USAGE MERGEs dominate this\n"
    "        -- loader's compile -- [1] MART_WAREHOUSE_EFFICIENCY_DAILY, [6] MART_TASK_GRAPH_DAILY and [6b]\n"
    "        -- MART_TASK_NODE_DAILY -- run every 4th Central hour (00, 04, 08, 12, 16, 20) instead of every\n"
    "        -- hour, and ALWAYS when d > 2: SP_NIGHTLY_RECONCILE DELETEs D-3..today of the first two and\n"
    "        -- re-loads them with ('HOURLY', 3), and backfills pass 90/365. The hourly task passes 2. A gated-off\n"
    "        -- arm is not a failure (req_fail / opt_fail untouched) and appends no :loaded token, so its\n"
    "        -- SOURCE_FRESHNESS_STATE row keeps its last stamp -- every name here contains DAILY, so the shared\n"
    "        -- 30h cadence rule never reads it stale. Every other arm below still runs every hour.\n"
    "        SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())) INTO :ct_hour;\n"
    "\n"
)

# (anchor that ends right before the arm's BEGIN, anchor that ends right after the arm's END;, label)
ARMS = (
    ("        -- [1] warehouse efficiency ------------------------------------------\n",
     "'MART_WAREHOUSE_EFFICIENCY_DAILY - other marts unaffected', CURRENT_ROLE();\n"
     "                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm "
     "swallow unchanged)\n        END;\n",
     "[1]"),
    ("        -- [6] task graphs -----------------------------------------------------\n",
     "'MART_TASK_GRAPH_DAILY - other marts unaffected', CURRENT_ROLE();\n"
     "                req_fail := req_fail + 1;   -- V066 #10: this REQUIRED arm failed (verdict-only; per-arm "
     "swallow unchanged)\n        END;\n",
     "[6]"),
    ("        -- same -:d window; MERGE on (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME).\n",
     "'MART_TASK_NODE_DAILY - other marts unaffected', CURRENT_ROLE();\n"
     "                opt_fail := opt_fail + 1;   -- V066 #10: this OPTIONAL arm failed (verdict-only; per-arm "
     "swallow unchanged)\n        END;\n",
     "[6b]"),
)


def gate_open(label: str) -> str:
    return f"        {GATE}   -- V159 (D5) gate {label}: every 4th Central hour; always when d > 2\n"


def gate_close(label: str) -> str:
    return f"        END IF;   -- V159 (D5) gate {label}\n"


marts = _insert_after(marts_base, M_DECL_ANCHOR, M_DECL, "DECLARE ct_hour")
marts = _insert_after(marts, M_HOUR_ANCHOR, M_HOUR, "HOURLY Central-hour read")
for open_anchor, close_anchor, label in ARMS:
    assert marts.count(open_anchor + "        BEGIN\n") == 1, f"{label}: the arm's BEGIN must follow its anchor"
    marts = _insert_after(marts, open_anchor, gate_open(label), f"{label} gate open")
    marts = _insert_after(marts, close_anchor, gate_close(label), f"{label} gate close")

# the gates sit in the HOURLY branch, after the hour read, each around exactly one arm
_hourly, _daily = marts.split("    IF (UPPER(:SCOPE) = 'DAILY') THEN\n", 1)
assert marts.count(GATE) == 3 and _hourly.count(GATE) == 3 and "ct_hour" not in _daily
assert marts.count("INTO :ct_hour;") == 1
_read = _hourly.index("INTO :ct_hour;")
assert _hourly.index("d := GREATEST(1, LEAST(COALESCE(DAYS_BACK, 2), 400))::INT;") < _read
assert _read < _hourly.index("ext_lo := (SELECT COALESCE(") < _hourly.index(GATE)
for token, label in (("wh_eff", "[1]"), ("graphs", "[6]"), ("task_node", "[6b]")):
    _open = _hourly.index(gate_open(label))
    _close = _hourly.index(gate_close(label))
    assert _open < _hourly.index(f"loaded := loaded || '{token} ';") < _close
    assert _hourly[_open:_close].count("MERGE INTO") == 1           # exactly one arm per gate
for token in ("qfam", "role_hr", "schema_hr", "tagcov", "alloc", "alloc_xdim", "timeline"):   # still hourly
    _pos = _hourly.index(f"loaded := loaded || '{token} ';")
    assert not any(_hourly.index(gate_open(lb)) < _pos < _hourly.index(gate_close(lb)) for _, _, lb in ARMS), token
# the freshness stamp and the RETURN stay ungated
assert _hourly.rindex("END IF;   -- V159 (D5) gate") < _hourly.index(
    "MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t")

# ---------------------------------------------------------------------------------------------------
# D6  SP_CHANGE_ATTRIBUTION()  (base V033)
# ---------------------------------------------------------------------------------------------------
attr_base = _one(r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_CHANGE_ATTRIBUTION\(\).*?\$\$;\n",
                 BASE_ATTR.read_text(encoding="utf-8"), "SP_CHANGE_ATTRIBUTION (V033)")
A_ANCHOR = "$$\nBEGIN\n"
A_GATE = (
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
attr = _insert_after(attr_base, A_ANCHOR, A_GATE, "SP_CHANGE_ATTRIBUTION gate")
assert attr.index(A_GATE) < attr.index("UPDATE DBA_MAINT_DB.OVERWATCH.WAREHOUSE_CHANGE_REGISTRY t")
assert attr.count("RETURN 'attribution pass complete';") == 1 and attr.count("RETURN '") == 2
# the probe's clock is the proc's own 7-day filter clock (an LTZ instant against an LTZ instant: no zone)
assert "AND CHANGE_SEEN_AT >= DATEADD('hour', -3, CURRENT_TIMESTAMP()))) THEN\n" in attr
assert "AND r.CHANGE_SEEN_AT >= DATEADD('day', -7, CURRENT_TIMESTAMP())\n" in attr
assert "CONVERT_TIMEZONE" not in attr

# ---------------------------------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------------------------------
MARKER_MARTS = ("-- >>> derived:SP_LOAD_MARTS_V27  (from V152; + the D5 Central 4-hour gate around HOURLY arms [1], "
                "[6] and [6b], always open when d > 2, V159)")
MARKER_ATTR = ("-- >>> derived:SP_CHANGE_ATTRIBUTION  (from V033; + the D6 recent-unattributed EXISTS gate, V159)")

DESCRIPTION = (
    "Loader compile diet (wave-2b rework, owner decisions D5 + D6): two procs stop recompiling a heavy "
    "ACCOUNT_USAGE statement every hour. SP_LOAD_MARTS_V27 (re-derived from V152) reads the Central hour once "
    "at the top of its HOURLY branch and runs the three DAY-grain arms [1] MART_WAREHOUSE_EFFICIENCY_DAILY, "
    "[6] MART_TASK_GRAPH_DAILY and [6b] MART_TASK_NODE_DAILY only in the 00/04/08/12/16/20 Central cycles, and "
    "always when d > 2 (the nightly reconcile re-loads D-3..today with (''HOURLY'', 3); backfills pass 90/365). "
    "A gated-off arm is not a failure and appends no freshness token; the three names contain DAILY, so the 30h "
    "cadence rule never reads them stale. Every other arm, the DAILY scope, the freshness stamp and the RETURN "
    "are byte-identical. SP_CHANGE_ATTRIBUTION (re-derived from V033) runs its unchanged UPDATE only while a "
    "WAREHOUSE_CHANGE_REGISTRY row seen in the last 3 hours is unattributed (~3 attempts per change, the first "
    "at ~07:07 as before), else returns ''attribution pass skipped''. Latency trade: today''s partial row in "
    "the three marts is up to 4h old (5h across the November DST night) on the Optimize idle and sizing panels, "
    "the Unit costs task-graph panel, the Operations node-timing board and SP_SLO_BREACH_SCAN (V096; "
    "SLO_OBJECTIVES is empty today); completed days are unaffected. Estimated saving ~42-44 compile-min/week "
    "of the ~166 the listed DIAG families total. No task, schedule, rule, table or grant change; no tail CALL."
)
assert "'" not in DESCRIPTION.replace("''", "")                  # every apostrophe doubled
assert len(DESCRIPTION.replace("''", "'")) <= 4000

out = f"""-- V159__loader_compile_diet.sql
--
-- Loader compile diet (wave-2b rework, owner decisions D5 + D6). DIAG_CS_SELF_COST (2026-09-26) put the ten
-- heaviest OVERWATCH scheduled compile families at ~166 min/week on WH_ALFA_ADMIN (a floor, not the whole
-- scheduled total). After the alert scan, the next-largest
-- families are this migration's two procs, which recompile a heavy ACCOUNT_USAGE statement every hour for
-- data that changes far less often (runs/week x average compile, measured):
--   * SP_LOAD_MARTS_V27 HOURLY arm [1] MERGE MART_WAREHOUSE_EFFICIENCY_DAILY: 175 x 9.2 s = 27.0 min
--     (QUERY_HISTORY x2 + WAREHOUSE_METERING_HISTORY x2); arm [6] MERGE MART_TASK_GRAPH_DAILY: 175 x 5.1 s
--     = 14.8 min (TASK_HISTORY x2 + QUERY_ATTRIBUTION_HISTORY); arm [6b] MERGE MART_TASK_NODE_DAILY
--     (TASK_HISTORY) sits below the panel's cut, unmeasured. 175 = 24 hourly runs x 7 + the nightly
--     reconcile's 7 re-calls.
--   * SP_CHANGE_ATTRIBUTION's UPDATE WAREHOUSE_CHANGE_REGISTRY: 168 x 5.1 s = 14.4 min (an 8-day
--     QUERY_HISTORY join every hour, although registry rows only arrive when the change scan runs).
--
-- D5  SP_LOAD_MARTS_V27(VARCHAR, FLOAT), re-derived from V152 (its current definer). The HOURLY branch
--     reads the Central hour ONCE (SELECT HOUR(CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP()))
--     INTO :ct_hour) and wraps arms [1], [6] and [6b] each in IF (d > 2 OR MOD(ct_hour, 4) = 0). They run in
--     the 00/04/08/12/16/20 Central cycles (6 of 24) and ALWAYS when d > 2: SP_NIGHTLY_RECONCILE (V064)
--     DELETEs D-3..today of MART_WAREHOUSE_EFFICIENCY_DAILY and MART_TASK_GRAPH_DAILY and re-loads them with
--     ('HOURLY', 3), so an hour-only gate would leave those days empty until the next gated hour; backfills
--     pass 90/365. TASK_LOAD_MARTS_V27_HOURLY passes ('HOURLY', 2), so d = 2 there. A gated-off arm counts
--     as OK (req_fail / opt_fail untouched) and appends no :loaded token, so the token-gated freshness MERGE
--     leaves its SOURCE_FRESHNESS_STATE row at its last stamp -- all three names contain DAILY, so the shared
--     30h cadence rule (health strip, freshness boards, NATIVE_ALERT_STALE_FACTS, OPS_PIPELINE_DEGRADED)
--     never reads them stale. The arms' own text is not touched (not even re-indented); every other HOURLY
--     arm, the DAILY scope, the freshness stamp and the RETURN are byte-identical to V152.
-- D6  SP_CHANGE_ATTRIBUTION(), re-derived from V033 (its only definer). An early-return guard runs the
--     unchanged UPDATE only IF EXISTS a WAREHOUSE_CHANGE_REGISTRY row with CHANGED_BY IS NULL and
--     CHANGE_SEEN_AT >= DATEADD('hour', -3, CURRENT_TIMESTAMP()). CHANGE_SEEN_AT is the scan's TIMESTAMP_LTZ
--     CURRENT_TIMESTAMP() stamp (V024/V109), so the probe uses the same clock as the proc's own 7-day filter
--     (an instant against an instant: no zone conversion). That gives ~3 hourly attempts per change, the
--     first at ~07:07 (27 min after the 06:40 scan) exactly as today; a skipped run returns
--     'attribution pass skipped (...)'.
--
-- Latency trade (disclosed):
--   * Today's partial row in the three marts is up to 4h old (5h across the November DST fall-back night,
--     when 01:00-02:00 Central repeats). Readers that see it later: the Optimize idle / sizing / remediation
--     panels and the idle headline, the Unit costs task-graph panel, the Operations node-timing board and
--     graph-roots failure counts, and SP_SLO_BREACH_SCAN (V096 reads MART_WAREHOUSE_EFFICIENCY_DAILY and
--     MART_TASK_NODE_DAILY), so a warehouse or task SLO breach can surface up to ~4h later -- SLO_OBJECTIVES
--     is empty today, so no SLO is affected yet. WAREHOUSE_METERING_HISTORY (~3h) and
--     QUERY_ATTRIBUTION_HISTORY (up to ~8h) already lag, which offsets part of it.
--   * Completed days are unaffected: the reconcile always re-loads D-3..today, and the 00 and 04 Central
--     cycles re-cover yesterday. SP_ALERT_SCAN_DAILY's COST_IDLE_OPPORTUNITY arm reads completed days only.
--   * A reconcile that fails mid-way now refills today at the next 4-hour slot, not the next hour.
--   * A manual CALL SP_LOAD_MARTS_V27('HOURLY', 2) outside those hours skips the three arms; pass 3 to force
--     them (snowflake/loader_chain_check.sql says so).
--   * Attribution: a row seen more than 3h ago is not retried unless a newer unattributed change arrives
--     inside its 7-day window (the unchanged UPDATE then retries all of them). Only an ACCOUNT_USAGE delay
--     beyond ~3h could lose an attribution that way.
--   * Pre-existing and NOT changed here: the -65/+5 min evidence window assumes an hourly snapshot, but the
--     snapshot is daily (06:40) or on demand (Operations Run scan), so only ALTERs made within ~65 min
--     before a scan are ever attributed. Fixing that changes behaviour and needs its own decision.
--
-- Estimated saving (ESTIMATES, DIAG family numbers): [1] and [6] drop from 175 to 49 runs/week (6 Central
-- cycles x 7 + the reconcile's 7): 27.0 -> 7.5 and 14.8 -> 4.2, about 30 compile-min/week, plus [6b]
-- (unmeasured). SP_CHANGE_ATTRIBUTION: 14.4 -> ~0.3-2.6 (168 small-table probes plus ~3 full UPDATEs on a
-- day with a warehouse change), about 12-14. Total ~42-44 of the ~166 the listed families total. Added: one scalar SELECT per
-- loader run (175/week, no table).
--
-- No task, schedule, rule, table or grant change and no tail CALL: the next hourly TASK_LOAD_HOURLY graph
-- runs both procs. Idempotent; safe to re-run. Owner applies in Snowsight after V158. This file never runs
-- from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20159, 'V159 requires V158 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 158) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{MARKER_MARTS}
{marts}
{MARKER_ATTR}
{attr}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 159 AS VERSION,
       '{DESCRIPTION}' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 159);
"""

# --- self-assertions -------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 2
for marker, create in (
    (MARKER_MARTS, "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27("),
    (MARKER_ATTR, "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_CHANGE_ATTRIBUTION("),
):
    assert out.count(marker) == 1 and out.count(create) == 1
    assert marker + "\n" + create in out                        # marker sits directly above its CREATE
for banned in ("CREATE TASK", "ALTER TASK", "ALERT_CONFIG", "CREATE TABLE", "ALTER TABLE", "CREATE OR REPLACE VIEW",
               "CREATE OR REPLACE FUNCTION", "GRANT ", "EXECUTE TASK"):
    assert banned not in out.replace(marts, "").replace(attr, ""), banned
_outside = out.replace(marts, "").replace(attr, "")
assert not re.search(r"^\s*CALL\b", _outside, re.M)            # no apply-time CALL
assert "EXCEPTION (-20159" in out and "IF (v < 158) THEN" in out
assert "SELECT 159 AS VERSION" in out and "WHERE VERSION = 159)" in out

target = Path(os.environ.get("V159_OUT") or (MIG / "V159__loader_compile_diet.sql"))
with target.open("w", encoding="utf-8", newline="\n") as fh:
    fh.write(out)
print(f"wrote {target} ({len(out)} chars)")
