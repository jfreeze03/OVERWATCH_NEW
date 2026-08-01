#!/usr/bin/env python3
r"""Forward-generate V066__alert_escalation_serverless_window_timeline_atomicity.sql.

Bug round 6 (2026-07-31): the 5 migration-proc findings, all re-derived from their LATEST
defs via extract_proc + count-asserted needle edits (never a hand-edit). Three procs:

  SP_ALERT_SCAN (from V062)
    #1 PIPE_COPY_FAILURES  -- the daily dedupe key omitted the severity band, so once a
       HIGH event existed for a table+day, the SAME row crossing the 10-failed-file
       CRITICAL threshold later that day was blocked by NOT EXISTS -> the CRITICAL
       ingestion-outage page never fired until the next calendar day. Add a CRIT/WARN band
       to the key (mirrors the SEC_CRED_EXPIRY EXPIRED/EXPIRING discriminator, V056 #12).
    #6 COST_SERVERLESS_CREEP -- THIS_WK (USAGE_DATE >= today-7) INCLUDED today's partial
       METERING_DAILY_HISTORY row (8 slots) vs PRIOR_WK 7 -> inflated WoW, false doubling.
       Exclude today (AND USAGE_DATE < CURRENT_DATE()), exactly as V065 did for COST_AI_CREEP.
    #11 COST_DEPT_BUDGET_PACE -- same severity-blind daily dedupe key: a MEDIUM->HIGH
       escalation (OVER_PCT crossing 3x threshold) was dropped for the rest of the day.
       Add a HIGH/MED band.
  SP_ALERT_SCAN_DAILY (from V065)
    #2 COST_CONTRACT_BREACH -- severity-blind WEEKLY dedupe key: a mid-week HIGH->CRITICAL
       crossing (DAYS_LEFT falling to <=14) was suppressed until the next week's key.
       Add a CRIT/WARN band. (COST_AI_CREEP shares the bare weekly key but has no
       within-bucket escalation, so it is left untouched.)
  SP_LOAD_MARTS_V27 (from V062)
    #3 MART_INCIDENT_TIMELINE arm [8] -- a non-atomic DELETE+INSERT under AUTOCOMMIT: the
       48h-window DELETE committed immediately, so a transient failure in the 4-way UNION
       INSERT left the trailing 48h BLANK until the next hourly rebuild -- an incident
       timeline empty mid-incident. Wrap DELETE+INSERT in BEGIN TRANSACTION ... COMMIT with
       ROLLBACK in the handler (the B34 FACT_TASK_DAILY wrap pattern already in this file).
    #10 FALSE SUCCESS -- each mart arm swallows its own failure and CONTINUES, but the terminal
       RETURN always claimed the marts loaded. REQUIRED/OPTIONAL arm-failure counters now drive
       a machine-readable verdict (MARTS OK / MARTS WITH ERRORS: n required, m optional); the
       per-arm swallow is unchanged (one failed arm still never aborts the rest).
    #11 FRESHNESS ADVANCES ON FAILURE -- the per-scope SOURCE_FRESHNESS_STATE MERGE bumped
       GENERATION and wrote the successful-arm STATUS across a static source group, so a source
       whose arm just failed still looked freshly loaded. Drive the MERGE from the sources that
       actually loaded (token present in :loaded) so the stamp advances for successes only, with
       per-source STATUS. See the _MART_ARMS block below (these two ADD to #3's edits).

Derivation law (see gen_v061..65): tests/test_v066_*.py byte-compares (regenerate to a temp
file via V066_OUT). Idempotent; apply AFTER V065. No new objects.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"

_V062 = "V062__loader_robustness_alert_split_webhook.sql"
_V065 = "V065__alert_run_rate_windows.sql"


def extract_proc(path: str, name: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    m = pat.findall(text)
    assert m, (path, name)
    return m[-1]


def _apply(body: str, old: str, new: str, count: int, name: str) -> str:
    n = body.count(old)
    assert n == count, f"{name}: needle x{n} (want {count}): {old[:70]!r}"
    return body.replace(old, new)


def derive(name: str) -> str:
    base, edits = EDITS[name]
    body = extract_proc(base, name)
    for old, new, cnt in edits:
        body = _apply(body, old, new, cnt, name)
    for g in GUARDS.get(name, []):
        assert g in body, f"{name}: guardrail vanished: {g[:70]!r}"
    return body


EDITS = {
    # ---- SP_ALERT_SCAN (hourly rules) from V062 --------------------------------------
    'SP_ALERT_SCAN': (_V062, [
        # #6 COST_SERVERLESS_CREEP: exclude today so both weeks are 7 complete days
        ("""            FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
            WHERE USAGE_DATE >= DATEADD('day', -14, CURRENT_DATE())
              AND SERVICE_TYPE NOT IN ('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER', 'AI_SERVICES')""",
         """            FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY
            WHERE USAGE_DATE >= DATEADD('day', -14, CURRENT_DATE())
              AND USAGE_DATE < CURRENT_DATE()   -- V066 #6: exclude today so THIS_WK/PRIOR_WK are equal 7 complete days (mirrors V065 COST_AI_CREEP)
              AND SERVICE_TYPE NOT IN ('WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER', 'AI_SERVICES')""", 1),
        # #1 PIPE_COPY_FAILURES: severity band so HIGH->CRITICAL (>=10 files) re-fires
        ("               c.RULE_ID || '|' || p.DB || '.' || p.SCH || '.' || p.TBL || '|' || TO_VARCHAR(CURRENT_DATE())",
         "               c.RULE_ID || '|' || p.DB || '.' || p.SCH || '.' || p.TBL || '|' || IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN') || '|' || TO_VARCHAR(CURRENT_DATE())  -- V066 #1: band matches the CRITICAL severity so a HIGH->CRITICAL crossing re-fires", 1),
        # #11 COST_DEPT_BUDGET_PACE: severity band so MEDIUM->HIGH (>=3x threshold) re-fires
        ("               c.RULE_ID || '|' || d.DEPARTMENT || '|' || TO_VARCHAR(CURRENT_DATE())",
         "               c.RULE_ID || '|' || d.DEPARTMENT || '|' || IFF(d.OVER_PCT >= c.THRESHOLD_NUM * 3, 'HIGH', 'MED') || '|' || TO_VARCHAR(CURRENT_DATE())  -- V066 #11: band matches the HIGH severity so a MEDIUM->HIGH crossing re-fires", 1),
    ]),
    # ---- SP_ALERT_SCAN_DAILY (daily rules) from V065 ---------------------------------
    'SP_ALERT_SCAN_DAILY': (_V065, [
        # #2 COST_CONTRACT_BREACH: severity band. Anchored on the p.DAYS_LEFT METRIC_VALUE
        # line so it does NOT touch COST_AI_CREEP's identical bare weekly key.
        ("""               p.DAYS_LEFT,
               c.RULE_ID || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))""",
         """               p.DAYS_LEFT,
               c.RULE_ID || '|' || IFF(p.DAYS_LEFT <= 14, 'CRIT', 'WARN') || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))  -- V066 #2: band matches the CRITICAL severity so a mid-week HIGH->CRITICAL crossing re-fires""", 1),
    ]),
    # ---- SP_LOAD_MARTS_V27 arm [8] atomicity from V062 -------------------------------
    'SP_LOAD_MARTS_V27': (_V062, [
        # #3a open a transaction before the 48h-window DELETE
        ("""        -- [8] incident timeline (rolling 48h window rebuild) -----------------
        BEGIN
            DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
            WHERE EVENT_TS >= DATEADD('hour', -48, CURRENT_TIMESTAMP());""",
         """        -- [8] incident timeline (rolling 48h window rebuild) -----------------
        BEGIN
            -- V066 #3: wrap the DELETE+INSERT in ONE transaction. Under AUTOCOMMIT the DELETE
            -- committed immediately, so a later failure in the 4-way UNION INSERT (a transient
            -- ACCOUNT_USAGE read / COMPANY_FOR_DATABASE UDF error) left the trailing 48h BLANK
            -- until the next hourly rebuild -- an incident timeline empty mid-incident. ROLLBACK
            -- on error restores the prior rows (the B34 FACT_TASK_DAILY wrap pattern).
            BEGIN TRANSACTION;
            DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE
            WHERE EVENT_TS >= DATEADD('hour', -48, CURRENT_TIMESTAMP());""", 1),
        # #3b commit after the last UNION arm, before the success flag
        ("""            WHERE CHANGE_SEEN_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP());
            loaded := loaded || 'timeline ';""",
         """            WHERE CHANGE_SEEN_AT >= DATEADD('hour', -48, CURRENT_TIMESTAMP());
            COMMIT;
            loaded := loaded || 'timeline ';""", 1),
        # #3c roll back in the handler (anchored on this arm's unique error context)
        ("""            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_INCIDENT_TIMELINE - other marts unaffected', CURRENT_ROLE();""",
         """            WHEN OTHER THEN
                ROLLBACK;   -- V066 #3: undo the 48h DELETE if the rebuild INSERT failed
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'MartLoader', 'mart_load_failed', :emsg, 'MART_INCIDENT_TIMELINE - other marts unaffected', CURRENT_ROLE();""", 1),
    ]),
}

# ---- SP_LOAD_MARTS_V27 #10 FALSE SUCCESS + #11 FRESHNESS-ON-FAILURE -------------------
# Layered onto the same proc as #3 (arm [8] atomicity). Two more re-derived fixes:
#   #10 (P1) FALSE SUCCESS -- each mart arm wraps its work in BEGIN/EXCEPTION that logs to
#       APP_ERROR_LOG and CONTINUES, but the terminal RETURN always claimed the marts loaded.
#       Track a REQUIRED-arm and an OPTIONAL-arm failure count (incremented in each arm's OWN
#       handler) and turn the RETURN into a machine-readable verdict. The per-arm swallow is
#       untouched (one failed arm still never aborts the rest) -- only the verdict changes.
#   #11 (P3) FRESHNESS ADVANCES ON FAILURE -- the per-scope SOURCE_FRESHNESS_STATE MERGE bumped
#       GENERATION and wrote the successful-arm STATUS across a STATIC source group, so a source
#       whose arm just failed still looked freshly loaded. Drive the MERGE from the sources that
#       ACTUALLY loaded (their per-arm token is present in :loaded, appended only on success) so
#       GENERATION/SNAPSHOT_TS advance for successful sources only, with per-source STATUS.
#
# REQUIRED = core daily facts/marts; OPTIONAL = tag-coverage, per-node timing, AI/Cortex arms
# (guarded external views). Conservative split: when unsure, REQUIRED, so failures surface.
_MART_ARMS = [
    ('MART_WAREHOUSE_EFFICIENCY_DAILY - other marts unaffected', 'req'),
    ('MART_QUERY_FAMILY_DAILY - other marts unaffected', 'req'),
    ('FACT_QUERY_ROLE_HOURLY - other marts unaffected', 'req'),
    ('FACT_QUERY_SCHEMA_HOURLY - other marts unaffected', 'req'),
    ('MART_TAG_COVERAGE_DAILY - other marts unaffected', 'opt'),
    ('MART_COST_ALLOCATION_DAILY - other marts unaffected', 'req'),
    ('FACT_COST_ALLOC_XDIM_DAILY - other marts unaffected', 'req'),
    ('MART_TASK_GRAPH_DAILY - other marts unaffected', 'req'),
    ('MART_TASK_NODE_DAILY - other marts unaffected', 'opt'),
    ('MART_INCIDENT_TIMELINE - other marts unaffected', 'req'),
    ('MART_SECURITY_POSTURE_DAILY - other marts unaffected', 'req'),
    ('FACT_AI_USAGE_DAILY (code views) - other marts unaffected', 'opt'),
    ('FACT_AI_USAGE_DAILY (functions view optional) - other marts unaffected', 'opt'),
]
_marts_edits = EDITS['SP_LOAD_MARTS_V27'][1]

# #10a: one counter increment per arm, appended inside that arm's OWN exception handler
# (anchored on its unique CONTEXT string -> count 1 each). Order after #3 so the timeline
# arm's SELECT line still resolves once (its handler gained a ROLLBACK, not a new SELECT).
for _ctx, _kind in _MART_ARMS:
    _counter = 'req_fail' if _kind == 'req' else 'opt_fail'
    _label = 'REQUIRED' if _kind == 'req' else 'OPTIONAL'
    _old = ("                SELECT 'MartLoader', 'mart_load_failed', :emsg, '"
            + _ctx + "', CURRENT_ROLE();")
    _new = _old + ("\n                " + _counter + " := " + _counter
                   + " + 1;   -- V066 #10: this " + _label
                   + " arm failed (verdict-only; per-arm swallow unchanged)")
    _marts_edits.append((_old, _new, 1))

# #10b: declare the two failure counters
_marts_edits.append((
    "DECLARE\n    emsg VARCHAR;\n    loaded VARCHAR DEFAULT '';\n    d INT;\n"
    "    ext_lo DATE;\n    ext_lo_hour TIMESTAMP_LTZ;",
    "DECLARE\n    emsg VARCHAR;\n    loaded VARCHAR DEFAULT '';\n    d INT;\n"
    "    ext_lo DATE;\n    ext_lo_hour TIMESTAMP_LTZ;\n"
    "    req_fail INT DEFAULT 0;   -- V066 #10: REQUIRED-arm (core fact/mart) failures this run\n"
    "    opt_fail INT DEFAULT 0;   -- V066 #10: OPTIONAL-arm (tag-cov, task-node, AI/Cortex) failures",
    1))

# #10c: the terminal verdict replaces the unconditional 'marts loaded' string
_marts_edits.append((
    "    RETURN 'V27 marts loaded (' || :SCOPE || ', ' || :d || 'd): ' || :loaded;",
    "    -- V066 #10 FALSE SUCCESS: the terminal RETURN used to always claim the marts loaded,\n"
    "    -- even when an arm's EXCEPTION handler swallowed a failure and continued. Return a\n"
    "    -- machine-readable verdict from the REQUIRED / OPTIONAL failure counters instead.\n"
    "    IF (req_fail = 0) THEN\n"
    "        RETURN 'MARTS OK (' || :SCOPE || ', ' || :d || 'd): ' || :loaded\n"
    "               || IFF(:opt_fail > 0, '[' || :opt_fail || ' optional failed]', '');\n"
    "    END IF;\n"
    "    RETURN 'MARTS WITH ERRORS: ' || :req_fail || ' required, ' || :opt_fail || ' optional ('\n"
    "           || :SCOPE || ', ' || :d || 'd): ' || :loaded;",
    1))

# #11: token-gated freshness stamp -- HOURLY scope group (9 sources)
_marts_edits.append((
    "        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t\n"
    "        USING (\n"
    "            SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT\n"
    "            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS\n"
    "            WHERE SOURCE_NAME IN ('MART_WAREHOUSE_EFFICIENCY_DAILY', 'MART_QUERY_FAMILY_DAILY',\n"
    "                                  'FACT_QUERY_ROLE_HOURLY', 'FACT_QUERY_SCHEMA_HOURLY',\n"
    "                                  'MART_TAG_COVERAGE_DAILY', 'MART_COST_ALLOCATION_DAILY',\n"
    "                                  'FACT_COST_ALLOC_XDIM_DAILY', 'MART_TASK_GRAPH_DAILY',\n"
    "                                  'MART_INCIDENT_TIMELINE')\n"
    "        ) s\n"
    "        ON t.SOURCE_NAME = s.SOURCE_NAME\n"
    "        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,\n"
    "            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,\n"
    "            STATUS = :loaded\n"
    "        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)\n"
    "        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, :loaded);",
    "        -- V066 #11 FRESHNESS ADVANCES ON FAILURE: stamp ONLY the sources whose arm actually\n"
    "        -- loaded this run. This MERGE used to advance GENERATION and write the successful-arm\n"
    "        -- list as STATUS across the whole STATIC group, so a source whose arm just failed\n"
    "        -- still looked freshly loaded. Each arm appends its token to :loaded only on its\n"
    "        -- success path, so gate the source set on token membership (ARRAY_CONTAINS over\n"
    "        -- SPLIT(:loaded)); a failed source is left untouched -- its prior generation/snapshot\n"
    "        -- stand, correctly reading as not-loaded-this-run -- and STATUS now carries that\n"
    "        -- source's own outcome.\n"
    "        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t\n"
    "        USING (\n"
    "            SELECT f.SOURCE_NAME, ANY_VALUE(f.LAST_LOAD_TS) AS LAST_LOAD_TS,\n"
    "                   ANY_VALUE(f.ROW_COUNT) AS ROW_COUNT, LISTAGG(m.TOKEN, ' ') AS STATUS\n"
    "            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS f\n"
    "            JOIN (\n"
    "                SELECT SOURCE_NAME, TOKEN FROM VALUES\n"
    "                    ('MART_WAREHOUSE_EFFICIENCY_DAILY', 'wh_eff'),\n"
    "                    ('MART_QUERY_FAMILY_DAILY', 'qfam'),\n"
    "                    ('FACT_QUERY_ROLE_HOURLY', 'role_hr'),\n"
    "                    ('FACT_QUERY_SCHEMA_HOURLY', 'schema_hr'),\n"
    "                    ('MART_TAG_COVERAGE_DAILY', 'tagcov'),\n"
    "                    ('MART_COST_ALLOCATION_DAILY', 'alloc'),\n"
    "                    ('FACT_COST_ALLOC_XDIM_DAILY', 'alloc_xdim'),\n"
    "                    ('MART_TASK_GRAPH_DAILY', 'graphs'),\n"
    "                    ('MART_INCIDENT_TIMELINE', 'timeline')\n"
    "                    AS srcmap(SOURCE_NAME, TOKEN)\n"
    "            ) m ON m.SOURCE_NAME = f.SOURCE_NAME\n"
    "            WHERE ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' '))\n"
    "            GROUP BY f.SOURCE_NAME\n"
    "        ) s\n"
    "        ON t.SOURCE_NAME = s.SOURCE_NAME\n"
    "        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,\n"
    "            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,\n"
    "            STATUS = s.STATUS\n"
    "        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)\n"
    "        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS);",
    1))

# #11: token-gated freshness stamp -- DAILY scope group (posture + AI; AI has two arms)
_marts_edits.append((
    "        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t\n"
    "        USING (\n"
    "            SELECT SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT\n"
    "            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS\n"
    "            WHERE SOURCE_NAME IN ('MART_SECURITY_POSTURE_DAILY', 'FACT_AI_USAGE_DAILY')\n"
    "        ) s\n"
    "        ON t.SOURCE_NAME = s.SOURCE_NAME\n"
    "        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,\n"
    "            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,\n"
    "            STATUS = :loaded\n"
    "        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)\n"
    "        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, :loaded);",
    "        -- V066 #11 FRESHNESS ADVANCES ON FAILURE (DAILY scope): same token-gated stamp.\n"
    "        -- Only posture / AI sources whose arm loaded advance; FACT_AI_USAGE_DAILY collapses\n"
    "        -- its two arms (ai_code, ai_functions) to one row via GROUP BY so the MERGE matches\n"
    "        -- its target exactly once.\n"
    "        MERGE INTO DBA_MAINT_DB.OVERWATCH.SOURCE_FRESHNESS_STATE t\n"
    "        USING (\n"
    "            SELECT f.SOURCE_NAME, ANY_VALUE(f.LAST_LOAD_TS) AS LAST_LOAD_TS,\n"
    "                   ANY_VALUE(f.ROW_COUNT) AS ROW_COUNT, LISTAGG(m.TOKEN, ' ') AS STATUS\n"
    "            FROM DBA_MAINT_DB.OVERWATCH.MART_SOURCE_FRESHNESS f\n"
    "            JOIN (\n"
    "                SELECT SOURCE_NAME, TOKEN FROM VALUES\n"
    "                    ('MART_SECURITY_POSTURE_DAILY', 'posture'),\n"
    "                    ('FACT_AI_USAGE_DAILY', 'ai_code'),\n"
    "                    ('FACT_AI_USAGE_DAILY', 'ai_functions')\n"
    "                    AS srcmap(SOURCE_NAME, TOKEN)\n"
    "            ) m ON m.SOURCE_NAME = f.SOURCE_NAME\n"
    "            WHERE ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' '))\n"
    "            GROUP BY f.SOURCE_NAME\n"
    "        ) s\n"
    "        ON t.SOURCE_NAME = s.SOURCE_NAME\n"
    "        WHEN MATCHED THEN UPDATE SET LAST_LOAD_TS = s.LAST_LOAD_TS, ROW_COUNT = s.ROW_COUNT,\n"
    "            SNAPSHOT_TS = CURRENT_TIMESTAMP(), GENERATION = COALESCE(t.GENERATION, 0) + 1,\n"
    "            STATUS = s.STATUS\n"
    "        WHEN NOT MATCHED THEN INSERT (SOURCE_NAME, LAST_LOAD_TS, ROW_COUNT, GENERATION, STATUS)\n"
    "        VALUES (s.SOURCE_NAME, s.LAST_LOAD_TS, s.ROW_COUNT, 1, s.STATUS);",
    1))

GUARDS = {
    'SP_ALERT_SCAN': [
        "PIPE_COPY_FAILURES", "COST_SERVERLESS_CREEP", "COST_DEPT_BUDGET_PACE",
        # untouched sibling rules must survive
        "SEC_BREAK_GLASS_USE",
    ],
    'SP_ALERT_SCAN_DAILY': [
        "COST_CONTRACT_BREACH", "COST_AI_CREEP", "COST_FORECAST_BREACH",
        # V065's run-rate window fixes must carry through untouched
        "NULLIF(COUNT(DISTINCT CASE WHEN DAY < CURRENT_DATE() THEN DAY END), 0) AS DAILY_RATE_USD",
        "AND DAY < CURRENT_DATE()   -- V065 rank3",
        # V064's contract-breach burn must survive
        "NULLIF(COUNT(DISTINCT DAY), 0)",
    ],
    'SP_LOAD_MARTS_V27': [
        "MART_INCIDENT_TIMELINE",
        "loaded := loaded || 'timeline ';",
        # #10 verdict counters + #11 token-gated freshness must survive re-derivation
        "req_fail INT DEFAULT 0;",
        "opt_fail INT DEFAULT 0;",
        "ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' '))",
    ],
}

alertscan = derive("SP_ALERT_SCAN")
alertdaily = derive("SP_ALERT_SCAN_DAILY")
marts = derive("SP_LOAD_MARTS_V27")

# ---- correctness assertions on the generated procs ----
# #6 serverless creep window
assert alertscan.count("AND USAGE_DATE < CURRENT_DATE()") == 1, "#6 serverless-creep excludes today"
# #1 pipe-copy band present, bare key gone
assert "IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN')" in alertscan, "#1 pipe-copy severity band"
assert "p.TBL || '|' || TO_VARCHAR(CURRENT_DATE())" not in alertscan, "#1 bare pipe-copy key gone"
# #11 dept-pace band present, bare key gone
assert "IFF(d.OVER_PCT >= c.THRESHOLD_NUM * 3, 'HIGH', 'MED')" in alertscan, "#11 dept-pace severity band"
assert "d.DEPARTMENT || '|' || TO_VARCHAR(CURRENT_DATE())" not in alertscan, "#11 bare dept-pace key gone"
# #2 contract-breach band; AI-creep's bare weekly key untouched (exactly one bare key left)
assert "IFF(p.DAYS_LEFT <= 14, 'CRIT', 'WARN')" in alertdaily, "#2 contract-breach severity band"
assert alertdaily.count("c.RULE_ID || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))") == 1, \
    "#2 only COST_AI_CREEP keeps the bare weekly key (contract-breach now banded)"
# #3 arm [8] transaction wrap
assert "BEGIN TRANSACTION;\n            DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE" in marts, "#3 BEGIN TRANSACTION before the 48h DELETE"
assert "CURRENT_TIMESTAMP());\n            COMMIT;\n            loaded := loaded || 'timeline ';" in marts, "#3 COMMIT after the rebuild INSERT"
assert "ROLLBACK;   -- V066 #3" in marts, "#3 ROLLBACK in the arm [8] handler"
# #10 false-success verdict: counters declared, unconditional 'loaded' RETURN gone
assert marts.count("req_fail INT DEFAULT 0;") == 1 and marts.count("opt_fail INT DEFAULT 0;") == 1, "#10 counters declared"
assert "V27 marts loaded (" not in marts, "#10 unconditional 'marts loaded' RETURN removed"
assert "RETURN 'MARTS OK (" in marts and "RETURN 'MARTS WITH ERRORS: " in marts, "#10 machine-readable verdict RETURN"
# one increment per mart arm, split 9 required / 4 optional (13 handlers total)
assert marts.count("req_fail := req_fail + 1;") == 9, "#10 nine REQUIRED-arm increments"
assert marts.count("opt_fail := opt_fail + 1;") == 4, "#10 four OPTIONAL-arm increments"
# #11 freshness advances only for actually-loaded sources (both scope MERGEs), per-source STATUS
assert marts.count("ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' '))") == 2, "#11 both freshness MERGEs token-gated"
assert marts.count("LISTAGG(m.TOKEN, ' ') AS STATUS") == 2, "#11 per-source STATUS from that source's own token(s)"
assert "STATUS = :loaded" not in marts, "#11 static successful-arm-list STATUS removed"
assert "WHERE SOURCE_NAME IN ('MART_WAREHOUSE_EFFICIENCY_DAILY'" not in marts, "#11 static HOURLY source list replaced"
assert "WHERE SOURCE_NAME IN ('MART_SECURITY_POSTURE_DAILY'" not in marts, "#11 static DAILY source list replaced"

out = f"""-- V066__alert_escalation_serverless_window_timeline_atomicity.sql
--
-- Bug round 6 (2026-07-31): 5 pre-existing migration-proc fixes across three procs, all
-- re-derived from their LATEST defs via outputs/gen_v066.py + count-asserted needle edits.
-- Idempotent; apply AFTER V065. No new objects.
--
--   SP_ALERT_SCAN (from V062):
--     #1  PIPE_COPY_FAILURES  -- daily dedupe key gains a CRIT/WARN band so a HIGH->CRITICAL
--         ingestion-outage crossing (>=10 failed files) re-fires instead of being swallowed
--         by the day's earlier HIGH event.
--     #6  COST_SERVERLESS_CREEP -- exclude today (AND USAGE_DATE < CURRENT_DATE()) so THIS_WK
--         and PRIOR_WK are equal 7-complete-day windows (was 8 vs 7 -> inflated WoW), mirroring
--         the V065 COST_AI_CREEP fix.
--     #11 COST_DEPT_BUDGET_PACE -- daily dedupe key gains a HIGH/MED band so a MEDIUM->HIGH
--         crossing re-fires.
--   SP_ALERT_SCAN_DAILY (from V065):
--     #2  COST_CONTRACT_BREACH -- weekly dedupe key gains a CRIT/WARN band so a mid-week
--         HIGH->CRITICAL crossing (DAYS_LEFT falling to <=14) re-fires (COST_AI_CREEP shares
--         the bare weekly key but has no within-bucket escalation, so it is left untouched).
--   SP_LOAD_MARTS_V27 (from V062):
--     #3  MART_INCIDENT_TIMELINE arm [8] -- the 48h-window DELETE+INSERT is wrapped in ONE
--         transaction (BEGIN TRANSACTION/COMMIT, ROLLBACK on error). Under AUTOCOMMIT the
--         DELETE committed immediately, so a transient INSERT failure blanked the trailing
--         48h of the incident timeline until the next hourly rebuild.
--     #10 FALSE SUCCESS -- every mart arm swallows its own failure (logs to APP_ERROR_LOG and
--         CONTINUES) but the terminal RETURN always claimed the marts loaded. REQUIRED-arm and
--         OPTIONAL-arm failure counters (incremented in each arm's own handler) now drive a
--         machine-readable verdict: 'MARTS OK ...' when zero required failures, else
--         'MARTS WITH ERRORS: <n> required, <m> optional ...'. Per-arm swallow unchanged.
--     #11 FRESHNESS ADVANCES ON FAILURE -- the per-scope SOURCE_FRESHNESS_STATE MERGE bumped
--         GENERATION and wrote the successful-arm STATUS across a static source group, so a
--         source whose arm just failed still looked freshly loaded. The MERGE is now driven by
--         the sources that actually loaded (their per-arm token is present in :loaded), so the
--         stamp advances for successful sources only, with per-source STATUS.
--
-- No smoke test required (deterministic alert logic + the B34 transaction-wrap pattern
-- already used elsewhere in this file). Byte-verified by tests/test_v066_alert_escalation.py.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20066, 'V066 requires V065 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 65) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_ALERT_SCAN  (#1 pipe-copy band, #6 serverless-creep window, #11 dept-pace band)
{alertscan}
-- >>> derived:SP_ALERT_SCAN_DAILY  (#2 contract-breach severity band on the weekly dedupe key)
{alertdaily}
-- >>> derived:SP_LOAD_MARTS_V27  (#3 incident-timeline arm [8] atomic rebuild)
{marts}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 66 AS VERSION,
       'Alert escalation + serverless window + timeline atomicity (bug round 6): SP_ALERT_SCAN dedupe keys for PIPE_COPY_FAILURES (#1) and COST_DEPT_BUDGET_PACE (#11) gain a severity band so a within-bucket HIGH->CRITICAL / MEDIUM->HIGH crossing re-fires; COST_SERVERLESS_CREEP excludes today so both weeks are 7 complete days (#6); SP_ALERT_SCAN_DAILY COST_CONTRACT_BREACH weekly key gains a severity band (#2); SP_LOAD_MARTS_V27 incident-timeline arm [8] DELETE+INSERT wrapped in one transaction so a failed rebuild cannot blank the trailing 48h (#3), the terminal RETURN reports a machine-readable MARTS OK / MARTS WITH ERRORS verdict from required/optional arm-failure counters instead of always claiming success (#10), and the per-scope freshness stamp advances only for sources whose arm actually loaded this run (#11). Re-derived from V062/V065; no new objects.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 66);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 3, "three procs re-defined"
assert "CREATE TABLE" not in out and "CREATE TASK" not in out, "no new objects"
assert "EXCEPTION (-20066" in out and "IF (v < 65) THEN" in out, "version guard"

target = Path(os.environ.get("V066_OUT") or (MIG / "V066__alert_escalation_serverless_window_timeline_atomicity.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
