#!/usr/bin/env python3
"""Forward-generate V045__task_monitoring_restored.sql.

Owner 2026-07-13: "i messed up. i meant getting rid of resource monitor,
not task monitoring. we need to add that back." V043's retirement is
reversed loader-side — while KEEPING V043's r25 alert teeth and V044's
UNKNOWN scope — and the OVERWATCH_RM resource monitor (the thing actually
suspending the app warehouse) drops instead.

Derivation law:
  * FACT_TASK_DAILY DDL verbatim from V002; MART_TASK_GRAPH_DAILY from V027
  * SP_LOAD_DAILY_FACTS verbatim from V041 (task arm + freshness row back)
  * SP_LOAD_MARTS_V27 / SP_LOAD_PLATFORM_SCORE / SP_PURGE_FACTS /
    SP_NIGHTLY_RECONCILE verbatim from V042
  * SP_REFRESH_EXEC_BOARD from V042 + V044's UNKNOWN-scope edit
  * SP_ALERT_SCAN from V023 + V043's [18]/[19] teeth arms (extracted from
    the V043 file itself) — [06] PIPE_TASK_FAILURES stays this time; 19 arms
  * MART_SOURCE_FRESHNESS view verbatim from V042's tail (24 sources)
tests/test_v045_restore.py re-derives everything and byte-compares.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"


def read(name: str) -> str:
    return (MIG / name).read_text(encoding="utf-8")


def extract_proc(path: str, name: str) -> str:
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    matches = pat.findall(read(path))
    assert matches, (path, name)
    return matches[-1]


def extract_block(text: str, start: str, end: str, incl_end: bool = True) -> str:
    i = text.index(start)
    j = text.index(end, i) + (len(end) if incl_end else 0)
    return text[i:j]


# --- table DDLs, verbatim ---------------------------------------------------
v002 = read("V002__facts.sql")
ddl_fact = extract_block(v002, "CREATE TRANSIENT TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY (",
                         ");\n")
v027 = read("V027__mart_family.sql")
ddl_mart = extract_block(v027, "CREATE TABLE IF NOT EXISTS DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY (",
                         ");\n")

# --- verbatim proc re-emits ---------------------------------------------------
body_daily = extract_proc("V041__loader_efficiency.sql", "SP_LOAD_DAILY_FACTS")
assert "FACT_TASK_DAILY" in body_daily                     # the restored arm
body_marts = extract_proc("V042__codex_r22.sql", "SP_LOAD_MARTS_V27")
assert "MART_TASK_GRAPH_DAILY" in body_marts and "'TASK_FAIL'" in body_marts
body_score = extract_proc("V042__codex_r22.sql", "SP_LOAD_PLATFORM_SCORE")
assert "FACT_TASK_DAILY" in body_score
body_purge = extract_proc("V042__codex_r22.sql", "SP_PURGE_FACTS")
body_recon = extract_proc("V042__codex_r22.sql", "SP_NIGHTLY_RECONCILE")

# --- board: V042 + the V044 UNKNOWN-scope edit -------------------------------
body_board = extract_proc("V042__codex_r22.sql", "SP_REFRESH_EXEC_BOARD")
old = "        SELECT 'ALFA' AS COMPANY UNION ALL SELECT 'Trexis' UNION ALL SELECT 'ALL'"
assert body_board.count(old) == 1
body_board = body_board.replace(old, old + "\n        UNION ALL SELECT 'UNKNOWN'  -- V044 (#18): the unmapped bucket is a first-class pill")

# --- alert scan: V023 + V043's teeth (extracted from the V043 file) ----------
body_scan = extract_proc("V023__prod_scoped_volume.sql", "SP_ALERT_SCAN")
v043 = read("V043__task_retirement_alert_teeth.sql")
teeth = extract_block(v043, "    -- [18] SEC_NEW_ADMIN_NETWORK",
                      "'rule COST_EGRESS_SPIKE - other rules unaffected', CURRENT_ROLE();\n    END;\n")
anchor = "\n    IF (fails > 0) THEN"
idx = body_scan.index(anchor) + 1
body_scan = body_scan[:idx] + teeth + body_scan[idx:]
old = "RETURN 'alert scan v8 complete (EXPIRATION_DATE): ' || (17 - :fails) || '/17 rule blocks ok';"
new = "RETURN 'alert scan v10 (V045: task rule restored + r25 teeth kept): ' || (19 - :fails) || '/19 rule blocks ok';"
assert body_scan.count(old) == 1
body_scan = body_scan.replace(old, new)
assert "-- [06] PIPE_TASK_FAILURES" in body_scan           # stays this time
assert "-- [18] SEC_NEW_ADMIN_NETWORK" in body_scan and "-- [19] COST_EGRESS_SPIKE" in body_scan

# --- freshness view: V042 tail verbatim (24 sources incl. both task tables) --
m = re.search(r"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.MART_SOURCE_FRESHNESS AS\n.*?;\n",
              read("V042__codex_r22.sql"), re.S)
assert m
view = m.group(0)
assert "FACT_TASK_DAILY" in view and "MART_TASK_GRAPH_DAILY" in view

out = []
out.append("""-- V045__task_monitoring_restored.sql — the owner's correction.
--
-- 2026-07-13: "i messed up. i meant getting rid of resource monitor, not
-- task monitoring. we need to add that back. that's my fault."
--
--   * Task monitoring returns loader-side: tables recreated (V002/V027
--     DDL), every proc V043 made task-free is re-derived back to its
--     task-inclusive V041/V042 body, PIPE_TASK_FAILURES re-enables, and
--     the fact refills 120 days from TASK_HISTORY.
--   * KEPT from V043: the r25 alert teeth ([18] SEC_NEW_ADMIN_NETWORK,
--     [19] COST_EGRESS_SPIKE) — the scan now runs 19 arms.
--   * KEPT from V044: the exec board's UNKNOWN scope.
--   * REMOVED instead: the OVERWATCH_RM resource monitor — the 30-credit
--     monthly cap that was suspending WH_ALFA_OVERWATCH mid-use (the real
--     source of the error storm).
--
-- Safe whether or not V043 ran: CREATE IF NOT EXISTS + CREATE OR REPLACE
-- + a windowed delete/insert refill are idempotent in both worlds.

-- >>> tables
""")
out.append(ddl_fact + "\n")
out.append(ddl_mart + "\n")
for name, body in (("SP_LOAD_DAILY_FACTS", body_daily), ("SP_LOAD_MARTS_V27", body_marts),
                   ("SP_REFRESH_EXEC_BOARD", body_board), ("SP_LOAD_PLATFORM_SCORE", body_score),
                   ("SP_PURGE_FACTS", body_purge), ("SP_NIGHTLY_RECONCILE", body_recon),
                   ("SP_ALERT_SCAN", body_scan)):
    out.append(f"-- >>> derived:{name}\n{body}\n")
out.append(f"-- >>> derived:MART_SOURCE_FRESHNESS\n{view}\n")
out.append("""-- >>> rules
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET ENABLED = TRUE
 WHERE RULE_ID = 'PIPE_TASK_FAILURES';

-- >>> resource monitor OUT (the owner's actual target)
ALTER WAREHOUSE IF EXISTS WH_ALFA_OVERWATCH SET RESOURCE_MONITOR = NULL;
DROP RESOURCE MONITOR IF EXISTS OVERWATCH_RM;

-- >>> refill (idempotent: rebuild the trailing 120d window either way)
DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
 WHERE DAY >= DATEADD('day', -120, CURRENT_DATE());
INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
    (DAY, DATABASE_NAME, SCHEMA_NAME, TASK_NAME, COMPANY, RUNS, FAILED, AVG_SEC, LAST_STATE, LAST_ERROR)
SELECT
    DATE(QUERY_START_TIME),
    DATABASE_NAME,
    SCHEMA_NAME,
    NAME,
    DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(DATABASE_NAME),
    COUNT(*),
    SUM(IFF(STATE = 'FAILED', 1, 0)),
    AVG(DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME)),
    MAX_BY(STATE, QUERY_START_TIME),
    MAX_BY(LEFT(COALESCE(ERROR_MESSAGE, ''), 500), QUERY_START_TIME)
FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
WHERE QUERY_START_TIME >= DATEADD('day', -120, CURRENT_DATE())
GROUP BY 1, 2, 3, 4, 5;

-- >>> first fills
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('DAILY', 90);
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(30);
CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 45 AS VERSION, 'owner correction: task monitoring restored loader-side (tables + procs + rule + 120d refill; r25 teeth and V044 UNKNOWN scope kept); OVERWATCH_RM resource monitor dropped (the actual removal target)' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 45);
""")
target = Path(os.environ.get("V045_OUT") or (MIG / "V045__task_monitoring_restored.sql"))
target.write_text("".join(out), encoding="utf-8")
print(f"wrote {target.name}: {len(''.join(out).splitlines())} lines")
