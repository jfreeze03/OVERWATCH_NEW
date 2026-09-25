#!/usr/bin/env python3
"""Forward-generate V155: keep the app's own statements out of the operator-stats collector.

Streamlit-in-Snowflake stamps EVERY statement the app runs with its own QUERY_TAG
({"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP", ...}) and overrides any tag the app sends
(owner diagnostic 2026-09-24, v4.591.0). Since v4.591.0 the app's ops_sql.query_optimization_triage
drops those statements through common.not_app_self_sql, but SP_LOAD_QUERY_OPERATOR_STATS -- whose
candidate cursor is documented as "query_optimization_triage's EXACT filter set ... so the two surfaces
agree" -- still excludes only the 'OVERWATCH%' prefix and the '%OVERWATCH_APP%' text, neither of which
ever matches a SiS-stamped app statement. So an app read that spills or scans >50 GB is hidden from
triage but still spends the collector's 250-per-run cap and shows on the Operator boards.

ONE insertion into the candidate cursor, re-derived from the CURRENT definer V147 (tests/test_proc_lineage.py);
everything else is byte-identical (tests/migrations/test_v155_* normalizes it back). No backfill: already
collected app rows age out inside the fact's 30-day retention. No task change and no tail CALL.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V147__operator_stats_identity_grain.sql"
SIS_FRAGMENT = '"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP"'   # == app.config.APP_SIS_QUERY_TAG_FRAGMENT

procs = re.findall(
    r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_QUERY_OPERATOR_STATS\(DAYS_BACK FLOAT\).*?\n\$\$;\n",
    BASE.read_text(encoding="utf-8"), re.S)
assert len(procs) == 1, f"SP_LOAD_QUERY_OPERATOR_STATS (V147): expected 1 definition, got {len(procs)}"
proc = procs[0]
ANCHOR = "          AND COALESCE(qh.QUERY_TAG, '') NOT LIKE 'OVERWATCH%'\n"
INSERT = (
    "          -- V155: Streamlit-in-Snowflake stamps the app's own statements with this tag (it overrides the\n"
    "          -- app's), so match it too - parity with ops_sql.query_optimization_triage (common.not_app_self_sql).\n"
    f"          AND NOT CONTAINS(COALESCE(qh.QUERY_TAG, ''), '{SIS_FRAGMENT}')\n"
)
assert proc.count(ANCHOR) == 1, "cursor anchor must occur exactly once"
proc = proc.replace(ANCHOR, ANCHOR + INSERT, 1)
assert "'" not in SIS_FRAGMENT

HEADER = """-- V155__operator_stats_sis_app_tag.sql
--
-- Keep OVERWATCH's own statements out of the QOIE Slice 2 operator-stats collector.
-- Streamlit-in-Snowflake stamps every statement the app runs with its own QUERY_TAG
-- ({"StreamlitName":"DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP", ...}) and overrides any tag the app sends
-- (owner diagnostic 2026-09-24). Since app v4.591.0, Operations query triage drops those statements;
-- SP_LOAD_QUERY_OPERATOR_STATS's candidate cursor (documented as triage's exact filter set) still
-- matched only the 'OVERWATCH%' prefix and '%OVERWATCH_APP%' text, which a SiS-stamped statement never
-- carries - so a heavy app read could land on the Operator boards while triage hid it.
--
-- Re-derives SP_LOAD_QUERY_OPERATOR_STATS from V147 (its current definition), byte-identical except ONE
-- added cursor predicate (test_v155 proves it). No schema change, no backfill (already collected app rows
-- age out inside the 30-day retention), no task change, no tail procedure run. Apply AFTER V154.
-- Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20155, 'V155 requires V154 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 154) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_LOAD_QUERY_OPERATOR_STATS (from V147; + Streamlit-in-Snowflake app-tag exclusion in the candidate cursor, V155)
"""

DESCRIPTION = (
    "Operator-stats collector ignores the app''s own statements: SP_LOAD_QUERY_OPERATOR_STATS re-derived from "
    "V147 (byte-identical except one candidate-cursor predicate) to also exclude statements carrying the "
    "QUERY_TAG Streamlit-in-Snowflake stamps on every app statement (StreamlitName = "
    "DBA_MAINT_DB.OVERWATCH.OVERWATCH_APP). SiS overrides the app''s own OVERWATCH tag, so the cursor''s "
    "OVERWATCH-prefix and OVERWATCH_APP-text filters never matched app traffic; Operations query triage "
    "already drops it (app v4.591.0), so the two surfaces agree again. No schema change, no backfill: "
    "already collected app rows age out inside the 30-day retention."
)
TAIL = (
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)\n"
    "SELECT 155 AS VERSION,\n"
    f"       '{DESCRIPTION}' AS DESCRIPTION\n"
    "WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 155);\n"
)

out = HEADER + proc + "\n" + TAIL
assert out.count("CREATE OR REPLACE PROCEDURE") == 1 and "CALL " not in out
target = Path(os.environ.get("V155_OUT") or (MIG / "V155__operator_stats_sis_app_tag.sql"))
target.write_text(out, encoding="utf-8", newline="\n")
print(f"wrote {target} ({len(out):,} chars)")
