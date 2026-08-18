#!/usr/bin/env python3
"""Forward-generate V090: retire the MART_SPEND_ROLLUP_DT Dynamic Table pilot.

V015 created MART_SPEND_ROLLUP_DT as a deliberately low-risk Dynamic Table to answer
the MERGE-vs-DT refresh-cost question with a measured number instead of an argument
("Additive: nothing reads it yet"). The pilot served its purpose: the app standardized
on scheduled-task MERGE marts, and the 2026-08-17 audit confirmed NOTHING reads the DT
— no app query, no procedure, no task, no downstream mart — yet as a Dynamic Table it
keeps auto-refreshing every ~6h (TARGET_LAG='6 hours') on WH_ALFA_ADMIN, spending
serverless credits for no consumer.

V090 drops it. Idempotent DROP ... IF EXISTS; no data loss (the rollup is trivially
derivable from FACT_METERING_DAILY, which is untouched). FACT_METERING_DAILY's
CHANGE_TRACKING (enabled in V015 for the DT) is deliberately LEFT ON — it is cheap and
a future change-tracking reader may rely on it. Owner applies in Snowsight after V089.
This file never runs from the app.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"

out = """-- V090__drop_spend_rollup_dt_pilot.sql
--
-- Retire the MART_SPEND_ROLLUP_DT Dynamic Table pilot (V015). It was a deliberate,
-- low-risk experiment to measure Dynamic-Table refresh cost vs the MERGE marts
-- ("Additive: nothing reads it yet"). The app standardized on scheduled-task marts;
-- the 2026-08-17 audit confirmed NOTHING reads the DT (no app query, procedure, task,
-- or downstream mart), yet it keeps auto-refreshing every ~6h on WH_ALFA_ADMIN,
-- spending serverless credits for no consumer. Drop it.
--
-- Idempotent DROP ... IF EXISTS; no data loss (the rollup is derivable from
-- FACT_METERING_DAILY, untouched). FACT_METERING_DAILY.CHANGE_TRACKING (enabled in
-- V015 for the DT) is left ON -- cheap, and a future reader could rely on it. Owner
-- applies in Snowsight after V089. This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20090, 'V090 requires V089 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 89) THEN
        RAISE not_ready;
    END IF;
END;
$$;

DROP DYNAMIC TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_SPEND_ROLLUP_DT;

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 90 AS VERSION,
       'Retire the MART_SPEND_ROLLUP_DT Dynamic Table pilot (V015): a low-risk MERGE-vs-DT refresh-cost experiment that nothing ever read; the app standardized on scheduled-task marts. As a Dynamic Table it kept auto-refreshing every ~6h on WH_ALFA_ADMIN with no consumer -- pure serverless waste. DROP ... IF EXISTS; no data loss (derivable from FACT_METERING_DAILY).' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 90);
"""

assert out.count("DROP DYNAMIC TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.MART_SPEND_ROLLUP_DT") == 1
assert "EXCEPTION (-20090" in out and "IF (v < 89) THEN" in out
assert "SELECT 90 AS VERSION" in out and "WHERE VERSION = 90)" in out
# A pure retirement migration creates NOTHING and touches no compute/monitor.
assert "CREATE " not in out and "ALTER " not in out
assert "CREATE TASK" not in out and "CREATE WAREHOUSE" not in out
assert "RESOURCE MONITOR" not in out
# Snowflake supports only plain $$ dollar-quoting, never a tagged $tag$ (owner hit this
# on V089 in Snowsight).
assert "$_v090_$" not in out and "$v090$" not in out

target = Path(os.environ.get("V090_OUT") or (MIG / "V090__drop_spend_rollup_dt_pilot.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
