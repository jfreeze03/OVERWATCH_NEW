#!/usr/bin/env python3
"""Forward-generate V076: mirror the app-side anomaly materiality gate in the
server SP_ANOMALY_SWEEP so the COST_ANOMALY_SWEEP alert path matches the display
twin (app/logic/anomaly.py). Re-derived byte-for-byte from the latest proc
definition (V023) plus five enumerated edits to the COST_ANOMALY_SWEEP block."""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V023__prod_scoped_volume.sql"


def extract_proc(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert matches, name
    return matches[-1]


proc = extract_proc(BASE.read_text(encoding="utf-8"), "SP_ANOMALY_SWEEP")

# Each edit targets the COST_ANOMALY_SWEEP block only; every other rule arm in
# the sweep (dynamic-table failures, fingerprint drift, org creep, volume drop,
# Cortex pre-explain) is byte-identical to V023.
edits = [
    # (A) declare the credit-price local used by the $-denominated floor.
    (
        "    zthr FLOAT;\n    ai_model VARCHAR;",
        "    zthr FLOAT;\n    credit_price FLOAT;\n    ai_model VARCHAR;",
    ),
    # (B) read the configured credit price (same source + default as SP_ALERT_SCAN).
    (
        "    SELECT COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :zthr\n"
        "    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n"
        "    WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED;\n"
        "\n"
        "    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS\n"
        "        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)\n"
        "    WITH series AS (",
        "    SELECT COALESCE(MAX(THRESHOLD_NUM), 3.5) INTO :zthr\n"
        "    FROM DBA_MAINT_DB.OVERWATCH.ALERT_CONFIG\n"
        "    WHERE RULE_ID = 'COST_ANOMALY_SWEEP' AND ENABLED;\n"
        "\n"
        "    -- V076: materiality floor mirrors the app-side warehouse anomaly gate\n"
        "    -- (app/logic/anomaly.py): flag on real money AND a real baseline, so an\n"
        "    -- idle warehouse cannot post a z+20 event on a trivial active day.\n"
        "    SELECT COALESCE(TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD', VALUE, NULL))), 3.68)\n"
        "      INTO :credit_price FROM DBA_MAINT_DB.OVERWATCH.SETTINGS;\n"
        "\n"
        "    INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS\n"
        "        (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)\n"
        "    WITH series AS (",
    ),
    # (C) active-day count + a SIGNED z (spike vs collapse) alongside the abs z.
    (
        "    latest AS (\n"
        "        SELECT s.SERIES, s.COMPANY, s.DAY, s.CREDITS, m.MED, m.MAD,\n"
        "               ABS(0.6745 * (s.CREDITS - m.MED) / NULLIF(m.MAD, 0)) AS ROBUST_Z\n"
        "        FROM series s\n"
        "        JOIN mad m ON m.SERIES = s.SERIES\n"
        "        WHERE s.DAY = (SELECT MAX(DAY) FROM series)\n"
        "    )",
        "    active AS (\n"
        "        SELECT SERIES, COUNT_IF(CREDITS > 0) AS ACTIVE_DAYS\n"
        "        FROM series GROUP BY 1\n"
        "    ),\n"
        "    latest AS (\n"
        "        SELECT s.SERIES, s.COMPANY, s.DAY, s.CREDITS, m.MED, m.MAD, a.ACTIVE_DAYS,\n"
        "               0.6745 * (s.CREDITS - m.MED) / NULLIF(m.MAD, 0) AS SIGNED_Z,\n"
        "               ABS(0.6745 * (s.CREDITS - m.MED) / NULLIF(m.MAD, 0)) AS ROBUST_Z\n"
        "        FROM series s\n"
        "        JOIN mad m ON m.SERIES = s.SERIES\n"
        "        JOIN active a ON a.SERIES = s.SERIES\n"
        "        WHERE s.DAY = (SELECT MAX(DAY) FROM series)\n"
        "    )",
    ),
    # (D) title distinguishes a collapse from a spike and shows the signed z.
    (
        "           l.SERIES || ' spent ' || ROUND(l.CREDITS, 1) || ' credits on ' ||\n"
        "               TO_VARCHAR(l.DAY) || ' (z=' || ROUND(l.ROBUST_Z, 1) || ')',",
        "           l.SERIES || IFF(l.SIGNED_Z < 0, ' collapsed to ', ' spiked to ') ||\n"
        "               ROUND(l.CREDITS, 1) || ' credits on ' ||\n"
        "               TO_VARCHAR(l.DAY) || ' (z=' || ROUND(l.SIGNED_Z, 1) || ')',",
    ),
    # (E) the gate: a spike needs a material DAY, a collapse a material BASELINE,
    #     and either needs >=10 active days for a real baseline. Replaces the old
    #     CREDITS >= 1 floor, which fired on trivial dollars.
    (
        "    FROM latest l\n"
        "    WHERE l.MAD > 0 AND l.ROBUST_Z >= :zthr AND l.CREDITS >= 1\n"
        "      AND NOT EXISTS (",
        "    FROM latest l\n"
        "    WHERE l.MAD > 0 AND l.ROBUST_Z >= :zthr\n"
        "      AND l.ACTIVE_DAYS >= 10\n"
        "      AND (\n"
        "          (l.SIGNED_Z > 0 AND l.CREDITS * :credit_price >= 50)\n"
        "          OR (l.SIGNED_Z < 0 AND l.MED * :credit_price >= 50)\n"
        "      )\n"
        "      AND NOT EXISTS (",
    ),
]

for old, new in edits:
    assert proc.count(old) == 1, f"anchor drifted:\n{old[:80]}"
    proc = proc.replace(old, new)

# Sanity: the untouched arms are still present, and the new gate landed.
for guard in (
    "PERF_FINGERPRINT_DRIFT",
    "COST_ORG_ACCOUNT_CREEP",
    "PIPE_VOLUME_DROP",
    "SNOWFLAKE.CORTEX.COMPLETE(:ai_model, :ai_prompt)",
    "l.ACTIVE_DAYS >= 10",
    "l.CREDITS * :credit_price >= 50",
    "l.MED * :credit_price >= 50",
):
    assert guard in proc, guard

out = f"""-- V076__anomaly_materiality_gate.sql
--
-- Mirror the app-side warehouse-anomaly materiality gate (app/logic/anomaly.py,
-- v4.147.0) in the server SP_ANOMALY_SWEEP so the COST_ANOMALY_SWEEP alert path
-- stops firing on trivial dollars, matching the display twin. Re-derived from the
-- latest definition (V023) with edits confined to the COST_ANOMALY_SWEEP block:
--   * a $-denominated floor (ANOMALY_MIN_USD 50) via the configured credit price,
--   * a minimum of 10 active days for a real baseline,
--   * a spike (positive z) gates on the day's own dollars while a collapse
--     (negative z) gates on the entity's baseline, so a stalled-pipeline drop
--     still fires while a trivial warehouse's active day does not.
-- Every other sweep arm (dynamic-table failures, fingerprint drift, org creep,
-- volume drop, Cortex pre-explain) is byte-identical to V023.
--
-- Owner applies in Snowsight after V075. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20076, 'V076 requires V075 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 75) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_ANOMALY_SWEEP  (materiality gate on the COST_ANOMALY_SWEEP arm)
{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 76 AS VERSION,
       'Anomaly materiality gate: SP_ANOMALY_SWEEP now gates COST_ANOMALY_SWEEP on a $50 floor (via the configured credit price), a minimum of 10 active days, and a spike-vs-collapse baseline test - mirroring the app-side flag_anomalies gate so the alert path matches the display. All other sweep arms unchanged.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 76);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE TABLE" not in out and "CREATE TASK" not in out
assert "EXCEPTION (-20076" in out and "IF (v < 75) THEN" in out
assert "SELECT 76 AS VERSION" in out and "WHERE VERSION = 76)" in out

target = Path(os.environ.get("V076_OUT") or (MIG / "V076__anomaly_materiality_gate.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
