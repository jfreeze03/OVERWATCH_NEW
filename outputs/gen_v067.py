#!/usr/bin/env python3
r"""Forward-generate V067__alert_attribution_onset_supersede_objectcost.sql.

Codex 50-rec review — the behavioral proc fixes (app-first batch shipped v4.110.0). Two
procs re-derived from their LATEST defs via extract_proc + count-asserted needle edits:

  SP_ALERT_SCAN (from V066)
    #22 COST_STORAGE_SURGE + PIPE_COPY_FAILURES classify an unknown database as 'ALFA' via
        IFF(name LIKE 'TRXS%', 'Trexis', 'ALFA'). Use the canonical COMPANY_FOR_DATABASE UDF
        (honors DEPARTMENT_MAP overrides + UNKNOWN) like the rest of the app.
    #20 COST_SERVERLESS_CREEP never fires on onset: prior-week 0 -> NULLIF -> NULL growth ->
        the row drops. Mirror COST_AI_CREEP's finite 999% onset sentinel so a brand-new
        serverless service FIRES.
    #40 Severity escalation left the lower-band OPEN alert open (a V066 follow-on): the band
        re-fires the higher severity as a NEW event but the prior WARN/MED event stayed OPEN,
        double-counting one incident in the severity tallies + score penalties. A global
        post-scan sweep resolves a WARN/MED event when its CRIT/HIGH sibling (the SAME dedupe
        key with only the band token swapped) is also OPEN. RESOLUTION_KIND='SUPERSEDED' is
        excluded from the per-rule precision score (which counts only ACTIONED/NOISE).
  SP_LOAD_OBJECT_COST (from V062)
    #10 On a failed load the proc ROLLs BACK + logs, then refreshes the freshness snapshot
        and RETURNs 'OK' -- a false success. Track the failure and RETURN a non-OK string so
        the failure is observable in the task return value / SP output.

Derivation law (see gen_v061..66): tests/test_v067_*.py byte-compares (regenerate via
V067_OUT). Idempotent; apply AFTER V066. No new objects.

Deferred to a later migration (each more involved): #16-mirror/#19 (alert forecast/pace
partial-day parity), #5 (task return-value gating), #21 (AI-predicate centralization),
#43 (new SP_INCIDENT_DECLARE proc), #4 (per-target transactions in SP_NIGHTLY_RECONCILE).
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"

_V062 = "V062__loader_robustness_alert_split_webhook.sql"
_V066 = "V066__alert_escalation_serverless_window_timeline_atomicity.sql"


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


# #40 global supersede sweep, appended just before SP_ALERT_SCAN's RETURN.
_SUPERSEDE = """    -- V067 #40: supersede the lower-severity OPEN event on escalation. V066's severity-band
    -- dedupe keys re-fire the HIGHER band as a NEW event but leave the prior lower-band event
    -- OPEN, double-counting one incident in the severity tallies + score penalties. Resolve a
    -- WARN/MED event when its CRIT/HIGH sibling (the SAME dedupe key with only the band token
    -- swapped) is also OPEN. RESOLUTION_KIND='SUPERSEDED' is excluded from the per-rule
    -- precision score (which counts only ACTIONED/NOISE), so it does not distort it. The band
    -- tokens '|WARN|'/'|MED|' occur only in the three banded keys, so this is a no-op for
    -- every other rule. Wrapped so a sweep failure never breaks the scan.
    BEGIN
        UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS lo
           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), RESOLUTION_KIND = 'SUPERSEDED'
         WHERE lo.STATUS = 'OPEN'
           AND EXISTS (
               SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS hi
               WHERE hi.STATUS = 'OPEN'
                 AND hi.RULE_ID = lo.RULE_ID
                 AND hi.DEDUPE_KEY <> lo.DEDUPE_KEY
                 AND (hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')
                      OR hi.DEDUPE_KEY = REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|'))
           );
    EXCEPTION
        WHEN OTHER THEN
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'AlertScan', 'supersede_sweep_failed', :emsg, 'V067 #40 escalation supersede - other rules unaffected', CURRENT_ROLE();
    END;

"""

_AI = ("SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0))")
_PRIOR = ("SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0))")

EDITS = {
    'SP_ALERT_SCAN': (_V066, [
        # #22a COST_STORAGE_SURGE company classification
        ("               IFF(g.DATABASE_NAME LIKE 'TRXS%', 'Trexis', 'ALFA'),",
         "               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(g.DATABASE_NAME),  -- V067 #22: honor overrides/UNKNOWN, not a raw TRXS%/ALFA guess", 1),
        # #22b PIPE_COPY_FAILURES company classification
        ("               IFF(p.DB LIKE 'TRXS%', 'Trexis', 'ALFA'),",
         "               DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(p.DB),  -- V067 #22: honor overrides/UNKNOWN, not a raw TRXS%/ALFA guess", 1),
        # #20 COST_SERVERLESS_CREEP onset sentinel (mirrors COST_AI_CREEP)
        (f"""                   ({_AI}
                    / NULLIF({_PRIOR}, 0)
                    - 1) * 100 AS GROWTH_PCT""",
         f"""                   -- V067 #20: onset (prior week 0) is an infinite ratio -> emit a finite 999%
                   -- sentinel so a brand-new serverless service FIRES (mirrors COST_AI_CREEP).
                   CASE WHEN {_PRIOR} = 0
                        THEN IFF({_AI} > 0, 999, 0)
                        ELSE ({_AI} / {_PRIOR} - 1) * 100 END AS GROWTH_PCT""", 1),
        # #40 supersede sweep before the RETURN
        ("""    END IF;

    RETURN 'alert scan v10""",
         "    END IF;\n\n" + _SUPERSEDE + "    RETURN 'alert scan v10", 1),
    ]),
    'SP_LOAD_OBJECT_COST': (_V062, [
        # #17a carry the executing WAREHOUSE_NAME through the query-attribution stage.
        # QUERY_ATTRIBUTION_HISTORY has no WAREHOUSE_NAME, so LEFT JOIN QUERY_HISTORY on
        # QUERY_ID (the V036 pattern). LEFT keeps a query whose history row is missing so
        # its credits still partition exactly; MAX() keeps ONE row per QUERY_ID (the split
        # + counts join rely on that cardinality). Columns get an `a.` qualifier now.
        ("""    CREATE OR REPLACE TEMPORARY TABLE DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_QA_STAGE AS
    SELECT QUERY_ID, MIN(START_TIME)::DATE AS DAY,
           SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
    WHERE START_TIME >= :lo
    GROUP BY QUERY_ID
    HAVING SUM(COALESCE(CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(CREDITS_USED_QUERY_ACCELERATION, 0)) > 0;""",
         """    CREATE OR REPLACE TEMPORARY TABLE DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_QA_STAGE AS
    SELECT a.QUERY_ID, MIN(a.START_TIME)::DATE AS DAY,
           -- V067 #17: carry the executing warehouse so the residual (unattributed-to-object)
           -- row can resolve its COMPANY from the warehouse instead of being forced to
           -- 'UNKNOWN'. QUERY_ATTRIBUTION_HISTORY has no WAREHOUSE_NAME, so LEFT JOIN
           -- QUERY_HISTORY on QUERY_ID (the V036 pattern); LEFT + MAX keeps one row per
           -- QUERY_ID and never drops a query whose history row is missing.
           MAX(q.WAREHOUSE_NAME) AS WAREHOUSE_NAME,
           SUM(COALESCE(a.CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0)) AS CREDITS
    FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY a
    LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
      ON q.QUERY_ID = a.QUERY_ID
     AND q.START_TIME >= :lo
    WHERE a.START_TIME >= :lo
    GROUP BY a.QUERY_ID
    HAVING SUM(COALESCE(a.CREDITS_ATTRIBUTED_COMPUTE, 0) + COALESCE(a.CREDITS_USED_QUERY_ACCELERATION, 0)) > 0;""", 1),
        # #17b resolve the residual COMPANY from the carried warehouse (was hardcoded
        # 'UNKNOWN'); the UDF falls back to 'UNKNOWN' only when the warehouse doesn't
        # resolve. Company joins the GROUP BY so residuals split by company, per-day
        # total unchanged (additive).
        ("""    SELECT qa.DAY, 'UNATTRIBUTED', 'RESIDUAL', 'QUERY_COMPUTE_RESIDUAL', 'UNKNOWN', SUM(qa.CREDITS)
    FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_QA_STAGE qa
    LEFT JOIN (SELECT DISTINCT QUERY_ID FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_OBJ_STAGE) obj_q
      ON obj_q.QUERY_ID = qa.QUERY_ID
    WHERE obj_q.QUERY_ID IS NULL
    GROUP BY 1;""",
         """    -- V067 #17: resolve the residual COMPANY from the executing WAREHOUSE_NAME
    -- (carried through OW_OBJCOST_QA_STAGE) via COMPANY_FOR_WAREHOUSE, so a
    -- company-filtered object-cost total reconciles when the warehouse identifies the
    -- company; COMPANY_FOR_WAREHOUSE returns 'UNKNOWN' only when the warehouse does not
    -- resolve. The company key joins the GROUP BY, so residual credits split by company
    -- while the per-day total stays additive.
    -- TODO(#18, deferred): storing BOTH the consumer-company (warehouse) and the
    -- object-owner-company is a separate dual-lens enhancement, not done here.
    SELECT qa.DAY, 'UNATTRIBUTED', 'RESIDUAL', 'QUERY_COMPUTE_RESIDUAL',
           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(qa.WAREHOUSE_NAME), SUM(qa.CREDITS)
    FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_QA_STAGE qa
    LEFT JOIN (SELECT DISTINCT QUERY_ID FROM DBA_MAINT_DB.OVERWATCH.OW_OBJCOST_OBJ_STAGE) obj_q
      ON obj_q.QUERY_ID = qa.QUERY_ID
    WHERE obj_q.QUERY_ID IS NULL
    GROUP BY 1, 5;""", 1),
        # #10a failure flag
        ("""DECLARE
    lo DATE;
    emsg VARCHAR;
BEGIN""",
         """DECLARE
    lo DATE;
    emsg VARCHAR;
    failed BOOLEAN DEFAULT FALSE;   -- V067 #10: a rolled-back load must return non-OK
BEGIN""", 1),
        # #10b set the flag in the handler
        ("""        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ObjectCost', 'object_cost_load_failed', :emsg, 'FACT_OBJECT_COST_DAILY - previous fill retained on rollback', CURRENT_ROLE();""",
         """        WHEN OTHER THEN
            ROLLBACK;
            emsg := SQLERRM;
            failed := TRUE;   -- V067 #10
            INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
            SELECT 'ObjectCost', 'object_cost_load_failed', :emsg, 'FACT_OBJECT_COST_DAILY - previous fill retained on rollback', CURRENT_ROLE();""", 1),
        # #10c conditional return (was an unconditional 'OK' even after a rollback)
        ("""    RETURN 'OK';
END;""",
         """    IF (failed) THEN
        RETURN 'FAILED: object-cost load rolled back - see APP_ERROR_LOG';
    END IF;
    RETURN 'OK';
END;""", 1),
    ]),
}

GUARDS = {
    'SP_ALERT_SCAN': [
        "COST_STORAGE_SURGE", "PIPE_COPY_FAILURES", "COST_SERVERLESS_CREEP",
        # V066 fixes must survive
        "IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN')",           # #1 pipe-copy band
        "AND USAGE_DATE < CURRENT_DATE()",                     # #6 serverless today-exclusion
    ],
    'SP_LOAD_OBJECT_COST': [
        "FACT_OBJECT_COST_DAILY", "BEGIN TRANSACTION",
    ],
}

alertscan = derive("SP_ALERT_SCAN")
objectcost = derive("SP_LOAD_OBJECT_COST")

# ---- correctness assertions ----
# #22 both rules use the UDF; no raw TRXS%/ALFA guess remains
assert alertscan.count("DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(") >= 2, "#22 both rules use the UDF"
assert "LIKE 'TRXS%', 'Trexis', 'ALFA'" not in alertscan, "#22 the raw TRXS%/ALFA guess is gone from these rules"
# #20 onset sentinel, old NULLIF-divisor form gone
assert alertscan.count("THEN IFF(SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) > 0, 999, 0)") == 1, "#20 serverless onset sentinel"
assert "/ NULLIF(SUM(IFF(USAGE_DATE < DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)), 0)" not in alertscan, "#20 old NULLIF divisor gone"
# #40 supersede sweep present, before the RETURN, band-swap matching
assert "RESOLUTION_KIND = 'SUPERSEDED'" in alertscan, "#40 supersede sweep"
assert "REPLACE(lo.DEDUPE_KEY, '|WARN|', '|CRIT|')" in alertscan and "REPLACE(lo.DEDUPE_KEY, '|MED|', '|HIGH|')" in alertscan, "#40 band-swap match"
assert alertscan.index("RESOLUTION_KIND = 'SUPERSEDED'") < alertscan.index("RETURN 'alert scan v10"), "#40 sweep runs before the RETURN"
# #17 residual company resolved from the executing warehouse, not hardcoded UNKNOWN
assert "MAX(q.WAREHOUSE_NAME) AS WAREHOUSE_NAME" in objectcost, "#17 warehouse carried through the QA stage"
assert "LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q\n      ON q.QUERY_ID = a.QUERY_ID" in objectcost, "#17 QA stage LEFT JOINs QUERY_HISTORY for the warehouse"
assert "'QUERY_COMPUTE_RESIDUAL', 'UNKNOWN', SUM(qa.CREDITS)" not in objectcost, "#17 residual COMPANY is no longer hardcoded UNKNOWN"
assert "'QUERY_COMPUTE_RESIDUAL',\n           DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(qa.WAREHOUSE_NAME), SUM(qa.CREDITS)" in objectcost, "#17 residual COMPANY resolved via COMPANY_FOR_WAREHOUSE"
assert "GROUP BY 1, 5;" in objectcost, "#17 resolved company joins the residual GROUP BY"
# #10 conditional return
assert "failed BOOLEAN DEFAULT FALSE" in objectcost and "failed := TRUE;" in objectcost, "#10 failure flag"
assert "IF (failed) THEN\n        RETURN 'FAILED: object-cost load rolled back" in objectcost, "#10 non-OK on failure"

out = f"""-- V067__alert_attribution_onset_supersede_objectcost.sql
--
-- Codex 50-rec review: behavioral proc fixes. Two procs re-derived from their latest defs
-- (SP_ALERT_SCAN from V066, SP_LOAD_OBJECT_COST from V062) via outputs/gen_v067.py +
-- count-asserted needle edits. Idempotent; apply AFTER V066. No new objects.
--
--   SP_ALERT_SCAN:
--     #22 COST_STORAGE_SURGE + PIPE_COPY_FAILURES use COMPANY_FOR_DATABASE (was a raw
--         IFF(name LIKE 'TRXS%','Trexis','ALFA') that mislabeled unknown DBs as ALFA).
--     #20 COST_SERVERLESS_CREEP emits a finite 999% onset sentinel when prior-week spend is
--         0 (was NULLIF -> NULL growth -> the new-service row silently dropped).
--     #40 a global post-scan sweep supersedes the lower-band OPEN alert when its higher-band
--         sibling is also open (RESOLUTION_KIND='SUPERSEDED'), closing the V066 escalation
--         follow-on where a HIGH->CRITICAL crossing left two OPEN events for one incident.
--   SP_LOAD_OBJECT_COST:
--     #10 returns a non-OK string after a rolled-back load (was an unconditional 'OK').
--
-- No smoke test required (deterministic alert logic + a proc return-string change);
-- byte-verified by tests/test_v067_alert_attribution.py.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20067, 'V067 requires V066 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 66) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_ALERT_SCAN  (#22 company UDF, #20 serverless onset, #40 escalation supersede)
{alertscan}
-- >>> derived:SP_LOAD_OBJECT_COST  (#10 non-OK return on a rolled-back load)
{objectcost}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 67 AS VERSION,
       'Alert attribution + serverless onset + escalation supersede + object-cost honesty (Codex review): SP_ALERT_SCAN COST_STORAGE_SURGE/PIPE_COPY_FAILURES use COMPANY_FOR_DATABASE not a raw TRXS%/ALFA guess (#22); COST_SERVERLESS_CREEP emits a 999 onset sentinel when prior week is 0 (#20); a post-scan sweep supersedes the lower-band OPEN alert when its higher-band sibling is open, RESOLUTION_KIND=SUPERSEDED (#40, the V066 escalation follow-on); SP_LOAD_OBJECT_COST returns non-OK after a rolled-back load (#10). Re-derived from V066/V062; no new objects.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 67);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 2, "two procs re-defined"
assert "CREATE TABLE" not in out and "CREATE TASK" not in out, "no new objects"
assert "EXCEPTION (-20067" in out and "IF (v < 66) THEN" in out, "version guard"

target = Path(os.environ.get("V067_OUT") or (MIG / "V067__alert_attribution_onset_supersede_objectcost.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
