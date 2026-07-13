#!/usr/bin/env python3
"""Forward-generate V043__task_retirement_alert_teeth.sql.

Derivation law: every proc below is the LATEST effective definition
(V041/V042/V023) re-emitted VERBATIM plus the enumerated edits in EDITS.
tests/test_v043_task_retirement.py re-derives each one from the origin
migration at test time and byte-compares against the shipped file.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"

def extract(path: str, proc: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{proc}\(.*?\n\$\$;\n",
        re.S)
    matches = pat.findall(text)
    assert matches, (path, proc)
    return matches[-1]

def apply(body: str, edits: list[tuple[str, str]], name: str) -> str:
    for old, new in edits:
        n = body.count(old)
        assert n == 1, f"{name}: needle x{n}: {old[:90]!r}"
        body = body.replace(old, new)
    return body

# --------------------------------------------------------------------------
# Enumerated edits per derived object
# --------------------------------------------------------------------------
E_DAILY = [
    # [1] the FACT_TASK_DAILY fill arm goes (retirement)
    ("""    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY WHERE DAY >= :lo_short::DATE;
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
    WHERE QUERY_START_TIME >= :lo_short::DATE
    GROUP BY 1, 2, 3, 4, 5;

""", ""),
    # [2] its freshness row goes
    ("""        UNION ALL
        SELECT 'FACT_TASK_DAILY', MAX(LOAD_TS), COUNT(*)
        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
""", ""),
]

E_MARTS = [
    # [1] the whole [6] task-graphs arm goes
]
E_BOARD = [
    ("""    tk_daily AS (
        SELECT COMPANY, DAY, SUM(RUNS) AS RUNS, SUM(FAILED) AS FAILED
        FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
        WHERE DAY >= DATEADD('day', -90, CURRENT_DATE())
        GROUP BY 1, 2
    ),""",
     """    tk_daily AS (
        -- V043: task monitoring retired — the KPI arms below emit no rows,
        -- and the board table keeps its shape.
        SELECT NULL::VARCHAR AS COMPANY, NULL::DATE AS DAY, 0 AS RUNS, 0 AS FAILED
        WHERE FALSE
    ),"""),
]
E_SCORE = [
    ("""        tk AS (
            SELECT DAY, SUM(RUNS) AS TASK_RUNS, SUM(FAILED) AS TASK_FAILED
            FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
            WHERE DAY >= DATEADD('day', -:d, CURRENT_DATE())
            GROUP BY DAY
        ),""",
     """        tk AS (
            -- V043: task monitoring retired — columns stay, zero-filled.
            SELECT NULL::DATE AS DAY, 0 AS TASK_RUNS, 0 AS TASK_FAILED
            WHERE FALSE
        ),"""),
]
E_PURGE = [
    ("""    DELETE FROM DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
""", ""),
    ("""    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
     WHERE DAY < DATEADD('day', -1 * :daily_days, CURRENT_DATE());
    total := total + SQLROWCOUNT;
""", ""),
]
E_RECON = [
    ("""    DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY
     WHERE DAY >= DATEADD('day', -3, CURRENT_DATE());
""", ""),
]

# --------------------------------------------------------------------------
body_daily = apply(extract("V041__loader_efficiency.sql", "SP_LOAD_DAILY_FACTS"), E_DAILY, "daily")
body_marts = extract("V042__codex_r22.sql", "SP_LOAD_MARTS_V27")
# [6] task-graphs arm: regex excise (comment + BEGIN..END block)
m = re.search(r"\n        -- \[6\] task graphs -+\n        BEGIN\n.*?'MART_TASK_GRAPH_DAILY - other marts unaffected', CURRENT_ROLE\(\);\n        END;\n", body_marts, re.S)
assert m, "marts [6] arm"
body_marts = body_marts[:m.start()] + "\n" + body_marts[m.end():]
# timeline TASK_FAIL union goes
m = re.search(r"\n            UNION ALL\n            SELECT QUERY_START_TIME, 'TASK_FAIL',\n.*?AND STATE = 'FAILED'\n(?=            UNION ALL)", body_marts, re.S)
assert m, "timeline TASK_FAIL arm"
body_marts = body_marts[:m.start()] + "\n" + body_marts[m.end():]
# freshness IN-list loses MART_TASK_GRAPH_DAILY
old = """'FACT_COST_ALLOC_XDIM_DAILY', 'MART_TASK_GRAPH_DAILY',
                                  'MART_INCIDENT_TIMELINE')"""
new = """'FACT_COST_ALLOC_XDIM_DAILY',
                                  'MART_INCIDENT_TIMELINE')"""
assert body_marts.count(old) == 1
body_marts = body_marts.replace(old, new)
for gone in ("FACT_TASK_DAILY", "MART_TASK_GRAPH_DAILY", "TASK_HISTORY"):
    assert gone not in body_marts, f"marts residual {gone}"

body_board = apply(extract("V042__codex_r22.sql", "SP_REFRESH_EXEC_BOARD"), E_BOARD, "board")
body_score = apply(extract("V042__codex_r22.sql", "SP_LOAD_PLATFORM_SCORE"), E_SCORE, "score")
body_purge = apply(extract("V042__codex_r22.sql", "SP_PURGE_FACTS"), E_PURGE, "purge")
body_recon = apply(extract("V042__codex_r22.sql", "SP_NIGHTLY_RECONCILE"), E_RECON, "recon")
for nm, b in (("daily", body_daily), ("board", body_board), ("score", body_score),
              ("purge", body_purge), ("recon", body_recon)):
    for gone in ("FACT_TASK_DAILY", "MART_TASK_GRAPH_DAILY"):
        assert gone not in b, f"{nm} residual {gone}"

# --- alert scan: V023 origin, [06] out, [18]+[19] in, tail 18/18 -----------
body_scan = extract("V023__prod_scoped_volume.sql", "SP_ALERT_SCAN")
m = re.search(r"\n    -- \[06\] PIPE_TASK_FAILURES\n    BEGIN\n.*?'rule PIPE_TASK_FAILURES - other rules unaffected', CURRENT_ROLE\(\);\n    END;\n", body_scan, re.S)
assert m, "scan [06]"
body_scan = body_scan[:m.start()] + "\n" + body_scan[m.end():]

NEW_ARMS = """    -- [18] SEC_NEW_ADMIN_NETWORK (V043 — the r25 panel, with teeth)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               nn.USER_NAME || ' logged in from new network ' || nn.CLIENT_IP,
               'First seen ' || nn.FIRST_SEEN || ' against a 90d baseline. Auth: '
                   || COALESCE(nn.AUTH_FACTOR, '?')
                   || '. Expected after travel/VPN/host changes; anything else is the finding.',
               nn.LOGINS,
               c.RULE_ID || '|' || nn.USER_NAME || '|' || nn.CLIENT_IP
        FROM cfg c
        JOIN (
            SELECT L.USER_NAME,
                   COALESCE(L.CLIENT_IP, '(none)') AS CLIENT_IP,
                   MIN(L.EVENT_TIMESTAMP) AS FIRST_SEEN,
                   COUNT(*) AS LOGINS,
                   MAX(L.FIRST_AUTHENTICATION_FACTOR) AS AUTH_FACTOR
            FROM SNOWFLAKE.ACCOUNT_USAGE.LOGIN_HISTORY L
            JOIN (
                SELECT DISTINCT GRANTEE_NAME
                FROM SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_USERS
                WHERE DELETED_ON IS NULL
                  AND ROLE IN ('SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS')
            ) A ON A.GRANTEE_NAME = L.USER_NAME
            WHERE L.EVENT_TIMESTAMP >= DATEADD('day', -90, CURRENT_TIMESTAMP())
            GROUP BY 1, 2
            HAVING MIN(L.EVENT_TIMESTAMP) >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
        ) nn
          ON c.RULE_ID = 'SEC_NEW_ADMIN_NETWORK'
         AND nn.LOGINS >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule SEC_NEW_ADMIN_NETWORK - other rules unaffected', CURRENT_ROLE();
    END;
    -- [19] COST_EGRESS_SPIKE (V043 — the r25 panel, with teeth)
    BEGIN
        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS
            (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WITH cfg AS (
            SELECT * FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG WHERE ENABLED
        )
        SELECT b.RULE_ID, b.COMPANY, b.SEVERITY, b.TITLE, b.DETAIL, b.METRIC_VALUE, b.DEDUPE_KEY
        FROM (
        SELECT c.RULE_ID, 'ALL', c.SEVERITY,
               'Egress ' || eg.GB_24H || ' GB in 24h (14d avg ' || eg.GB_AVG_14D || ' GB/day)',
               'Top destination: ' || COALESCE(eg.TOP_REGION, '(same region)')
                   || '. Source: DATA_TRANSFER_HISTORY - drill in Security -> Egress.',
               eg.GB_24H,
               c.RULE_ID || '|' || TO_VARCHAR(CURRENT_DATE())
        FROM cfg c
        JOIN (
            SELECT ROUND(SUM(IFF(START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP()),
                                 BYTES_TRANSFERRED, 0)) / POWER(1024, 3), 1) AS GB_24H,
                   ROUND(SUM(BYTES_TRANSFERRED) / POWER(1024, 3) / 14, 1) AS GB_AVG_14D,
                   MAX_BY(TARGET_REGION, BYTES_TRANSFERRED) AS TOP_REGION
            FROM SNOWFLAKE.ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
            WHERE START_TIME >= DATEADD('day', -14, CURRENT_TIMESTAMP())
        ) eg
          ON c.RULE_ID = 'COST_EGRESS_SPIKE'
         AND eg.GB_24H >= c.THRESHOLD_NUM

        ) b (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)
        WHERE NOT EXISTS (
            SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e
            WHERE e.DEDUPE_KEY = b.DEDUPE_KEY
        );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            fails := fails + 1;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'rule_block_failed', :emsg,
                   'rule COST_EGRESS_SPIKE - other rules unaffected', CURRENT_ROLE();
    END;
"""
# append the new arms right before the OPS_SCAN_DEGRADED tail
anchor = "\n    IF (fails > 0) THEN"
if anchor not in body_scan:
    # fall back: locate the degraded-tail comment
    anchor = None
    m = re.search(r"\n    -- .*OPS_SCAN_DEGRADED.*\n", body_scan)
    assert m, "scan tail anchor"
    idx = m.start() + 1
else:
    idx = body_scan.index(anchor) + 1
body_scan = body_scan[:idx] + NEW_ARMS + body_scan[idx:]
old = "RETURN 'alert scan v8 complete (EXPIRATION_DATE): ' || (17 - :fails) || '/17 rule blocks ok';"
new = "RETURN 'alert scan v9 (V043 task retirement + r25 teeth): ' || (18 - :fails) || '/18 rule blocks ok';"
assert body_scan.count(old) == 1
body_scan = body_scan.replace(old, new)
assert "FACT_TASK_DAILY" not in body_scan and "PIPE_TASK_FAILURES" not in body_scan

# --- freshness view: V042 tail re-emit minus the two task sources ----------
v042 = (MIG / "V042__codex_r22.sql").read_text(encoding="utf-8")
m = re.search(r"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.MART_SOURCE_FRESHNESS AS\n.*?;\n", v042, re.S)
assert m, "freshness view"
view = m.group(0)
for src in ("FACT_TASK_DAILY", "MART_TASK_GRAPH_DAILY"):
    mm = re.search(rf"UNION ALL\nSELECT '{src}', MAX\(LOAD_TS\), COUNT\(\*\),\n.*?\nFROM DBA_MAINT_DB\.OVERWATCH\.{src}\n", view, re.S)
    assert mm, src
    view = view[:mm.start()] + view[mm.end():]
assert "FACT_TASK_DAILY" not in view and "MART_TASK_GRAPH_DAILY" not in view

# --------------------------------------------------------------------------
out = []
out.append("""-- V043__task_retirement_alert_teeth.sql — finish what v4.42.0 started.
-- Authority: docs/reviews/CODEX_R27_ADJUDICATION_20260713.md (#1 + H1/H5).
--
--   * Loader-side task monitoring retires: SP_LOAD_DAILY_FACTS stops
--     scanning TASK_HISTORY, the [6] task-graphs mart arm and the
--     timeline's TASK_FAIL union go, board/score task inputs zero-fill
--     (table shapes unchanged), purge/reconcile/freshness drop the two
--     tables, then the tables drop.
--   * PIPE_TASK_FAILURES (HIGH) — alerting on task failures this whole
--     time — is disabled and its scan arm removed.
--   * The r25 security metrics get teeth: SEC_NEW_ADMIN_NETWORK and
--     COST_EGRESS_SPIKE rules + scan arms (same dedupe shape as every arm).
--
-- Derivation law: SP_LOAD_DAILY_FACTS from V041; SP_LOAD_MARTS_V27,
-- SP_REFRESH_EXEC_BOARD, SP_LOAD_PLATFORM_SCORE, SP_PURGE_FACTS,
-- SP_NIGHTLY_RECONCILE from V042; SP_ALERT_SCAN from V023; the freshness
-- view from V042's tail — each verbatim + enumerated edits, re-derived and
-- byte-compared in tests/test_v043_task_retirement.py.
-- Procs swap under the running graph (no task surgery); drops come last.

""")
for name, body in (("SP_LOAD_DAILY_FACTS", body_daily), ("SP_LOAD_MARTS_V27", body_marts),
                   ("SP_REFRESH_EXEC_BOARD", body_board), ("SP_LOAD_PLATFORM_SCORE", body_score),
                   ("SP_PURGE_FACTS", body_purge), ("SP_NIGHTLY_RECONCILE", body_recon),
                   ("SP_ALERT_SCAN", body_scan)):
    out.append(f"-- >>> derived:{name}\n{body}\n")
out.append(f"-- >>> derived:MART_SOURCE_FRESHNESS\n{view}\n")
out.append("""-- >>> rules
-- The task rule retires (kept as a disabled row: history stays attributable).
UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG
   SET ENABLED = FALSE
 WHERE RULE_ID = 'PIPE_TASK_FAILURES';

MERGE INTO DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG t
USING (
    SELECT * FROM VALUES
        ('SEC_NEW_ADMIN_NETWORK', 'SECURITY', 'Admin login from a network unseen in 90 days', TRUE, 'HIGH',   1, 24),
        ('COST_EGRESS_SPIKE',     'COST',     'Outbound transfer above threshold (GB / 24h)', TRUE, 'MEDIUM', 100, 24)
    AS s(RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
) s
ON t.RULE_ID = s.RULE_ID
WHEN NOT MATCHED THEN INSERT (RULE_ID, FAMILY, NAME, ENABLED, SEVERITY, THRESHOLD_NUM, WINDOW_HOURS)
     VALUES (s.RULE_ID, s.FAMILY, s.NAME, s.ENABLED, s.SEVERITY, s.THRESHOLD_NUM, s.WINDOW_HOURS);

-- >>> retire
DELETE FROM DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE
 WHERE SOURCE_NAME IN ('FACT_TASK_DAILY', 'MART_TASK_GRAPH_DAILY');
DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE WHERE KIND = 'TASK_FAIL';
DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.FACT_TASK_DAILY;
DROP TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_TASK_GRAPH_DAILY;

-- >>> first fills (procs already swapped; nothing here references the drops)
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27('HOURLY', 3);
CALL DBA_MAINT_DB.OVERWATCH.SP_REFRESH_EXEC_BOARD();
CALL DBA_MAINT_DB.OVERWATCH.SP_LOAD_PLATFORM_SCORE(30);
CALL DBA_MAINT_DB.OVERWATCH.SP_ALERT_SCAN();

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 43 AS VERSION, 'task retirement finished loader-side (fills/board/score/purge/reconcile/freshness + both tables dropped, PIPE_TASK_FAILURES disabled) + r25 metrics get alert teeth (SEC_NEW_ADMIN_NETWORK, COST_EGRESS_SPIKE)' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 43);
""")
target = Path(os.environ.get("V043_OUT") or (MIG / "V043__task_retirement_alert_teeth.sql"))
target.write_text("".join(out), encoding="utf-8")
print(f"wrote {target.name}: {len(''.join(out).splitlines())} lines")
