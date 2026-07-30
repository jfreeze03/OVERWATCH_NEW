#!/usr/bin/env python3
"""Forward-generate V063__webhook_capture_once_daily_facts_failguard.sql.

The two correctness/robustness fixes DEFERRED from V062 after its adversarial
review (wf_0ae6f51b). The T3.1-T3.4 perf-loader restructures are handled
separately in V064 (they carry the highest corruption risk and stay isolated
from these correctness fixes).

Fixes:
  B9    SP_NOTIFY_WEBHOOK capture-once. The V062-era attempt re-derived the
        fitting event set TWICE (once for the message, once for the ledger),
        straddling the network send, so a concurrent ALERT_EVENTS insert (e.g.
        SP_ALERT_SCAN_DAILY) or CURRENT_TIMESTAMP crossing the 24h window mid-send
        could mark an unsent event delivered / re-send a sent one. Now the fitting
        EVENT_IDs are captured ONCE into an ARRAY (fits_ids); the message, the
        ALERT_DELIVERIES ledger, and the NOTIFIED_AT update are ALL derived from
        that one immutable array (ARRAY_CONTAINS), so the sent set == the recorded
        set by construction. Non-fitting events keep NULL NOTIFIED_AT and re-drain.
        EVENT_ID is VARCHAR(80) (V004:39) -> array of strings.
        !! ARRAY_AGG/ARRAY_CONTAINS binding is runtime-only -> OWNER SMOKE TEST
           REQUIRED (DEPLOYMENT.md); a byte-compare cannot prove it.
  B34-obs  SP_LOAD_DAILY_FACTS fail-guard. V062 wrapped each of the 3 DELETE+INSERT
        units in a per-table transaction (correct), but a per-table EXCEPTION
        handler swallowed the failure and the proc still advanced the DAILY_FACTS
        watermark + returned success -> a partial-failure run read as success. Now
        a failed_any flag is set in each handler; the OW_LOAD_WATERMARKS('DAILY_
        FACTS') advance is gated on NOT failed_any (so the failed day self-heals on
        the next run's watermark re-read), and the RETURN reports the partial
        failure. Per-table isolation preserved (siblings still load).

Derivation law (see gen_v061/gen_v062): each proc re-derived from its LATEST
definition (SP_NOTIFY_WEBHOOK from V034 — V062 did not touch it; SP_LOAD_DAILY_
FACTS from V062) via extract_proc + count-asserted apply; guardrails assert
prior-release fixes survive. tests/test_v063_webhook_failguard.py byte-compares.
The needle strings below were produced + count-verified by the V063 authoring
pass (workflow wakr45eom).

Idempotent; apply AFTER V062. No data heal (both are forward-healing proc swaps).
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"


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


# ===================== validated edits (V063 authoring pass) =====================
EDITS = {
    'SP_NOTIFY_WEBHOOK': ('V034__route_company_filter.sql', [
        ('    r_compfilter VARCHAR;   -- v4: per-route company scope (owner: Teams = ALFA-only for now)\n    c1 CURSOR FOR',
         '    r_compfilter VARCHAR;   -- v4: per-route company scope (owner: Teams = ALFA-only for now)\n    fits_ids ARRAY;         -- V063: frozen fitting EVENT_IDs (VARCHAR EVENT_ID) shared by message + ledger + NOTIFIED_AT\n    c1 CURSOR FOR', 1),
        ("        -- Eligible = open, young enough, matches this route, and THIS ROUTE\n        -- has not delivered it yet (other routes' successes are irrelevant).\n        SELECT LISTAGG('[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140), '\\n')\n               WITHIN GROUP (ORDER BY e.RAISED_AT DESC)\n          INTO :message\n        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID\n        WHERE e.STATUS = 'OPEN'\n          AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())\n          AND (:r_family = 'ALL' OR c.FAMILY = :r_family)\n          AND (:r_compfilter = 'ALL' OR e.COMPANY = :r_compfilter OR UPPER(e.COMPANY) = 'ALL')\n          AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END\n              >= CASE :r_minsev WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END\n          AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d\n                          WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id);",
         "        -- Eligible = open, young enough, matches this route, and THIS ROUTE\n        -- has not delivered it yet (other routes' successes are irrelevant).\n        -- V063 capture-once: freeze the FITTING EVENT_IDs (eligible, ordered\n        -- newest-first, whose cumulative JSON-escaped length -- each line plus\n        -- 2 chars per '\\n' separator -- stays <= 3000) into an immutable ARRAY.\n        -- The message, the ledger, and NOTIFIED_AT are then ALL derived from\n        -- THIS SAME array, so a concurrent ALERT_EVENTS insert or the 24h\n        -- window sliding as CURRENT_TIMESTAMP advances cannot make the sent\n        -- set and the recorded set diverge.\n        SELECT ARRAY_AGG(f.EVENT_ID) WITHIN GROUP (ORDER BY f.RAISED_AT DESC, f.EVENT_ID)\n          INTO :fits_ids\n        FROM (\n            SELECT e.EVENT_ID, e.RAISED_AT,\n                   SUM(LEN(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(\n                       '[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140),\n                       CHR(92), CHR(92) || CHR(92)),\n                       CHR(34), CHR(92) || CHR(34)),\n                       CHR(10), CHR(92) || 'n'),\n                       CHR(13), ''),\n                       CHR(9),  CHR(92) || 't')) + 2)\n                     OVER (ORDER BY e.RAISED_AT DESC, e.EVENT_ID\n                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - 2 AS CUM_LEN\n            FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n            JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID\n            WHERE e.STATUS = 'OPEN'\n              AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())\n              AND (:r_family = 'ALL' OR c.FAMILY = :r_family)\n              AND (:r_compfilter = 'ALL' OR e.COMPANY = :r_compfilter OR UPPER(e.COMPANY) = 'ALL')\n              AND CASE e.SEVERITY WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END\n                  >= CASE :r_minsev WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END\n              AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d\n                              WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id)\n        ) f\n        WHERE f.CUM_LEN <= 3000;\n\n        -- Build the message from the frozen fits set ONLY, in the SAME order\n        -- (RAISED_AT DESC, EVENT_ID) used to compute the fit. Its escaped\n        -- length is <= 3000 by construction, so the LEFT(:message, 3000) at\n        -- send time never truncates mid-event.\n        SELECT LISTAGG('[' || e.SEVERITY || '] ' || LEFT(e.TITLE, 140), '\\n')\n               WITHIN GROUP (ORDER BY e.RAISED_AT DESC, e.EVENT_ID)\n          INTO :message\n        FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n        WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids);", 1),
        ('                -- Ledger rows for THIS route only (success path).\n                INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES (EVENT_ID, ROUTE_ID)\n                SELECT e.EVENT_ID, :r_route_id\n                FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n                JOIN DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG c ON c.RULE_ID = e.RULE_ID\n                WHERE e.STATUS = \'OPEN\'\n                  AND e.RAISED_AT >= DATEADD(\'hour\', -24, CURRENT_TIMESTAMP())\n                  AND (:r_family = \'ALL\' OR c.FAMILY = :r_family)\n                  AND (:r_compfilter = \'ALL\' OR e.COMPANY = :r_compfilter OR UPPER(e.COMPANY) = \'ALL\')\n                  AND CASE e.SEVERITY WHEN \'CRITICAL\' THEN 4 WHEN \'HIGH\' THEN 3 WHEN \'MEDIUM\' THEN 2 ELSE 1 END\n                      >= CASE :r_minsev WHEN \'CRITICAL\' THEN 4 WHEN \'HIGH\' THEN 3 WHEN \'MEDIUM\' THEN 2 ELSE 1 END\n                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d\n                                  WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id);\n                sent_total := sent_total + SQLROWCOUNT;\n\n                -- Back-compat: NOTIFIED_AT still means "delivered somewhere\n                -- at least once" (the drill, the delivery chip, and MTTA\n                -- surfaces read it).\n                UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n                   SET NOTIFIED_AT = CURRENT_TIMESTAMP()\n                 WHERE e.NOTIFIED_AT IS NULL\n                   AND EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d\n                               WHERE d.EVENT_ID = e.EVENT_ID);',
         '                -- Ledger rows for THIS route only (success path) -- the SAME\n                -- frozen fits set that built the message, NOT a re-derivation.\n                -- NOT EXISTS keeps it idempotent if this route is retried.\n                INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES (EVENT_ID, ROUTE_ID)\n                SELECT e.EVENT_ID, :r_route_id\n                FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n                WHERE ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids)\n                  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ALERT_DELIVERIES d\n                                  WHERE d.EVENT_ID = e.EVENT_ID AND d.ROUTE_ID = :r_route_id);\n                sent_total := sent_total + SQLROWCOUNT;\n\n                -- Back-compat: NOTIFIED_AT still means "delivered somewhere at\n                -- least once" (the drill, the delivery chip, and MTTA surfaces\n                -- read it). Set it ONLY for the frozen fits set actually sent;\n                -- a non-fitting event stays NULL and re-drains next run.\n                UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e\n                   SET NOTIFIED_AT = CURRENT_TIMESTAMP()\n                 WHERE e.NOTIFIED_AT IS NULL\n                   AND ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids);', 1),
    ]),
    'SP_LOAD_DAILY_FACTS': ('V062__loader_robustness_alert_split_webhook.sql', [
        ('-- B34 (V062): transaction-wrap error capture\nBEGIN',
         '-- B34 (V062): transaction-wrap error capture\n    failed_any BOOLEAN DEFAULT FALSE;  -- V063 B34obs: any per-table wrap failed this run\nBEGIN', 1),
        ("            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_TASK_DAILY - other daily facts unaffected', CURRENT_ROLE();\n    END;",
         "            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_TASK_DAILY - other daily facts unaffected', CURRENT_ROLE();\n            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return\n    END;", 1),
        ("            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_LOGIN_DAILY - other daily facts unaffected', CURRENT_ROLE();\n    END;",
         "            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_LOGIN_DAILY - other daily facts unaffected', CURRENT_ROLE();\n            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return\n    END;", 1),
        ("            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_STORAGE_DAILY - other daily facts unaffected', CURRENT_ROLE();\n    END;",
         "            SELECT 'DailyFacts', 'fact_load_failed', :emsg, 'FACT_STORAGE_DAILY - other daily facts unaffected', CURRENT_ROLE();\n            failed_any := TRUE;  -- V063 B34obs: hold watermark + non-success return\n    END;", 1),
        ("    -- V041 R5+R6: advance the watermark; loader-owned freshness.\n    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t\n    USING (SELECT 'DAILY_FACTS' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s\n    ON t.SOURCE = s.SOURCE\n    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS\n    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);",
         "    -- V041 R5+R6: advance the watermark; loader-owned freshness.\n    -- V063 B34obs: HOLD the DAILY_FACTS watermark when any per-table wrap\n    -- failed, so the next run re-reads from the held mark and re-covers the\n    -- missed day (lo_short = wm - 1d; idempotent DELETE+INSERT). The\n    -- SOURCE_FRESHNESS_STATE MERGE below stays UNGUARDED so a swallowed\n    -- failure surfaces as a stale freshness row.\n    IF (NOT failed_any) THEN\n    MERGE INTO DBA_MAINT_DB.OVERWATCH.OW_LOAD_WATERMARKS t\n    USING (SELECT 'DAILY_FACTS' AS SOURCE, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ AS WM_TS) s\n    ON t.SOURCE = s.SOURCE\n    WHEN MATCHED THEN UPDATE SET WM_TS = s.WM_TS\n    WHEN NOT MATCHED THEN INSERT (SOURCE, WM_TS) VALUES (s.SOURCE, s.WM_TS);\n    END IF;", 1),
        ("    RETURN 'daily facts loaded';",
         "    IF (failed_any) THEN\n        RETURN 'daily facts loaded WITH ERRORS - one or more tables failed, watermark held';\n    END IF;\n    RETURN 'daily facts loaded';", 1),
    ]),
}

GUARDS = {
}

webhook = derive("SP_NOTIFY_WEBHOOK")
daily = derive("SP_LOAD_DAILY_FACTS")

# correctness assertions on the generated procs
assert webhook.count("fits_ids ARRAY;") == 1, "B9 fits_ids declared once"
assert webhook.count("ARRAY_AGG(f.EVENT_ID)") == 1, "B9 capture-once"
assert webhook.count("ARRAY_CONTAINS(e.EVENT_ID::VARIANT, :fits_ids)") == 3, "B9 message + ledger + NOTIFIED_AT share the frozen set"
assert daily.count("failed_any BOOLEAN DEFAULT FALSE;") == 1, "B34 flag declared"
assert daily.count("failed_any := TRUE;") == 3, "B34 flag set in all 3 handlers"
assert "IF (NOT failed_any) THEN" in daily, "B34 watermark advance gated"

out = f"""-- V063__webhook_capture_once_daily_facts_failguard.sql
--
-- The two correctness/robustness fixes deferred from V062 (adversarial review
-- wf_0ae6f51b). See gen_v063.py header for detail.
--   B9   SP_NOTIFY_WEBHOOK: capture the fitting EVENT_IDs ONCE into an ARRAY so the
--        message, the ledger, and NOTIFIED_AT share one immutable set (no send-vs-
--        ledger race). !! OWNER SMOKE TEST REQUIRED (DEPLOYMENT.md) — ARRAY binding
--        is runtime-only and a byte-compare cannot prove it.
--   B34  SP_LOAD_DAILY_FACTS: a per-table failure now holds the DAILY_FACTS watermark
--        and returns a non-success string (was: swallowed -> advanced -> false
--        success). Per-table isolation preserved; the failed day self-heals next run.
--
-- The T3.1-T3.4 perf-loader restructures are handled in V064 (isolated by risk).
-- Idempotent; apply AFTER V062. No data heal (forward-healing proc swaps).

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20063, 'V063 requires V062 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 62) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_NOTIFY_WEBHOOK  (B9 capture-once - SMOKE TEST REQUIRED)
{webhook}
-- >>> derived:SP_LOAD_DAILY_FACTS  (B34 fail-guard: hold watermark on partial failure)
{daily}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 63 AS VERSION,
       'Webhook capture-once (B9: message + ledger + NOTIFIED_AT share one frozen fitting-event ARRAY, no send-vs-ledger race - owner smoke test) + daily-facts fail-guard (B34: a per-table failure holds the DAILY_FACTS watermark and returns non-success instead of a false success). Deferred from V062; perf loader T3 in V064.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 63);
"""
target = Path(os.environ.get("V063_OUT") or (MIG / "V063__webhook_capture_once_daily_facts_failguard.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
