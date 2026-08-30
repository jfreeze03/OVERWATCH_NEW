#!/usr/bin/env python3
"""Forward-generate V097: SP_ANOMALY_SWEEP mean-absolute-deviation fallback when MAD=0.

[MED] SP_ANOMALY_SWEEP misses material cost spikes on majority-idle series. The
COST_ANOMALY_SWEEP arm (V076) computes the robust modified z as
`0.6745*(CREDITS-MED)/NULLIF(MAD,0)` and then requires `WHERE l.MAD > 0`. When the
baseline dispersion collapses (MAD=0 -- common for intermittent / majority-idle serverless
series, or a steady-constant series), the row is silently dropped and NO anomaly event is
raised, even for a large material spike. The authoritative app twin
app/logic/anomaly.py robust_zscores falls back to a mean-absolute-deviation denominator
(_MEANAD_K * dev / mean_ad) when mad==0, precisely so "a single spike cannot hide itself";
the server never got that fallback.

Fix -- port the app estimator into the arm, matching its constants and gate order exactly:
  * add a `meanad` sibling CTE: per-series AVG(ABS(CREDITS-MED))  (== abs_dev.mean())
  * pick the denominator MAD-first, mean-AD-second, with the matching constant:
      IFF(MAD>0, 0.6745, 0.7979) * (CREDITS-MED) / NULLIF(IFF(MAD>0, MAD, MEAN_AD), 0)
    (0.6745 == _MAD_K, 0.7979 == _MEANAD_K; both-zero -> NULL z, never fires -- mirrors the
     Python else-zeros branch)
  * drop the hard `WHERE l.MAD > 0`, keep a chosen-denominator>0 guard (SIGNED_Z IS NOT NULL)
The z<0 collapse suppression and the materiality gates ($50 floor via :credit_price,
>=10 active days, spike-vs-collapse) are unchanged.

Procedure re-derivation only, no schema change, no backfill. Owner applies in Snowsight after
V096; forward-healing (next daily sweep starts catching these). This file never runs from the app.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
BASE = MIG / "V076__anomaly_materiality_gate.sql"


def extract_procedure(text: str, name: str) -> str:
    pattern = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\$\$;\n",
        re.S,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"{name}: expected 1 proc, got {len(matches)}"
    return matches[0]


# edit 1: add a mean-absolute-deviation sibling CTE right after the mad CTE.
MAD_CTE_OLD = (
    "    mad AS (\n"
    "        SELECT s.SERIES, m.MED, MEDIAN(ABS(s.CREDITS - m.MED)) AS MAD\n"
    "        FROM series s JOIN med m ON m.SERIES = s.SERIES\n"
    "        GROUP BY 1, 2\n"
    "    ),"
)
MAD_CTE_NEW = (
    MAD_CTE_OLD + "\n"
    "    -- V097: mean-absolute-deviation fallback denominator (== abs_dev.mean() in the\n"
    "    -- app twin app/logic/anomaly.py robust_zscores) for series whose MAD collapses to 0.\n"
    "    meanad AS (\n"
    "        SELECT s.SERIES, AVG(ABS(s.CREDITS - m.MED)) AS MEAN_AD\n"
    "        FROM series s JOIN med m ON m.SERIES = s.SERIES\n"
    "        GROUP BY 1\n"
    "    ),"
)

# edit 2: join the sibling CTE in the `latest` CTE.
JOIN_OLD = (
    "        JOIN mad m ON m.SERIES = s.SERIES\n"
    "        JOIN active a ON a.SERIES = s.SERIES"
)
JOIN_NEW = (
    "        JOIN mad m ON m.SERIES = s.SERIES\n"
    "        JOIN meanad ma ON ma.SERIES = s.SERIES\n"
    "        JOIN active a ON a.SERIES = s.SERIES"
)

# edit 3: MAD-first / mean-AD-second denominator + constant (mirrors the Python gate order).
Z_OLD = (
    "               0.6745 * (s.CREDITS - m.MED) / NULLIF(m.MAD, 0) AS SIGNED_Z,\n"
    "               ABS(0.6745 * (s.CREDITS - m.MED) / NULLIF(m.MAD, 0)) AS ROBUST_Z"
)
Z_NEW = (
    "               IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)\n"
    "                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0) AS SIGNED_Z,\n"
    "               ABS(IFF(m.MAD > 0, 0.6745, 0.7979) * (s.CREDITS - m.MED)\n"
    "                   / NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0)) AS ROBUST_Z"
)

# edit 4: drop the hard MAD>0 filter, keep a chosen-denominator>0 guard.
WHERE_OLD = "    WHERE l.MAD > 0 AND l.ROBUST_Z >= :zthr"
WHERE_NEW = "    WHERE l.SIGNED_Z IS NOT NULL AND l.ROBUST_Z >= :zthr"

proc = extract_procedure(BASE.read_text(encoding="utf-8"), "SP_ANOMALY_SWEEP")
for old in (MAD_CTE_OLD, JOIN_OLD, Z_OLD, WHERE_OLD):
    assert proc.count(old) == 1, f"expected 1 of {old!r}, got {proc.count(old)}"
proc = (proc.replace(MAD_CTE_OLD, MAD_CTE_NEW)
            .replace(JOIN_OLD, JOIN_NEW)
            .replace(Z_OLD, Z_NEW)
            .replace(WHERE_OLD, WHERE_NEW))
# fixes landed
assert "meanad AS (" in proc and "AVG(ABS(s.CREDITS - m.MED)) AS MEAN_AD" in proc
assert "JOIN meanad ma ON ma.SERIES = s.SERIES" in proc
assert "IFF(m.MAD > 0, 0.6745, 0.7979)" in proc
assert "NULLIF(IFF(m.MAD > 0, m.MAD, ma.MEAN_AD), 0)" in proc
assert "WHERE l.SIGNED_Z IS NOT NULL AND l.ROBUST_Z >= :zthr" in proc
# the hard MAD>0 drop is gone
assert "WHERE l.MAD > 0" not in proc
# untouched anchors: collapse suppression + all materiality gates
for anchor in (
    "AND l.ACTIVE_DAYS >= 10",
    "(l.SIGNED_Z > 0 AND l.CREDITS * :credit_price >= 50)",
    "OR (l.SIGNED_Z < 0 AND l.MED * :credit_price >= 50)",
    "'COST_ANOMALY_SWEEP|' || l.SERIES || '|' || TO_VARCHAR(l.DAY)",
):
    assert anchor in proc, anchor

out = f"""-- V097__anomaly_mean_ad_fallback.sql
--
-- SP_ANOMALY_SWEEP mean-absolute-deviation fallback when MAD=0. The COST_ANOMALY_SWEEP arm
-- (V076) computed the robust modified z as 0.6745*(CREDITS-MED)/NULLIF(MAD,0) and then hard-
-- filtered `WHERE l.MAD > 0`, so a majority-idle / intermittent series whose baseline
-- dispersion collapses to MAD=0 was silently dropped -- even for a large material spike. The
-- authoritative app twin app/logic/anomaly.py robust_zscores falls back to a mean-absolute-
-- deviation denominator (_MEANAD_K * dev / mean_ad) when mad==0, so a single spike cannot hide
-- itself; the server never got that fallback.
--
-- Re-derives SP_ANOMALY_SWEEP from V076 with the app estimator ported in, matching its
-- constants and gate order: a meanad sibling CTE (AVG(ABS(CREDITS-MED)) == abs_dev.mean());
-- the denominator is MAD-first (0.6745/MAD) else mean-AD (0.7979/MEAN_AD); both-zero yields a
-- NULL z that never fires. The hard MAD>0 filter is replaced by SIGNED_Z IS NOT NULL. The z<0
-- collapse suppression and the materiality gates ($50 floor, >=10 active days, spike-vs-
-- collapse) are unchanged.
--
-- Procedure re-derivation only, no schema change, no backfill. Owner applies in Snowsight
-- after V096; forward-healing (next daily sweep). This file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20097, 'V097 requires V096 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 96) THEN
        RAISE not_ready;
    END IF;
END;
$$;

{proc}
INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 97 AS VERSION,
       'SP_ANOMALY_SWEEP mean-AD fallback: COST_ANOMALY_SWEEP re-derived from V076 so a series whose median-absolute-deviation collapses to 0 (intermittent / majority-idle serverless, or steady-constant) no longer silently drops its spike. Adds a meanad sibling CTE (AVG(ABS(CREDITS-MED))) and picks the robust-z denominator MAD-first (0.6745/MAD) else mean-AD (0.7979/MEAN_AD), mirroring app.logic.anomaly.robust_zscores constants + gate order; drops the hard WHERE MAD>0 for a SIGNED_Z IS NOT NULL guard. Collapse suppression + materiality gates ($50, >=10 active days) unchanged. Proc only, no backfill; forward-healing on the next sweep.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 97);
"""

# ---- self-assertions ------------------------------------------------------------
assert out.count("CREATE OR REPLACE PROCEDURE") == 1
assert "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_ANOMALY_SWEEP" in out
assert "CREATE OR REPLACE VIEW" not in out and "CREATE OR REPLACE FUNCTION" not in out
assert "CREATE TABLE" not in out and "ALTER TABLE" not in out and "CREATE TASK" not in out
assert "INSERT OVERWRITE" not in out
assert "WHERE l.SIGNED_Z IS NOT NULL AND l.ROBUST_Z >= :zthr" in out  # the hard MAD>0 drop is gone (see proc assert)
assert "meanad AS (" in out and "0.7979" in out
assert "EXCEPTION (-20097" in out and "IF (v < 96) THEN" in out
assert "SELECT 97 AS VERSION" in out and "WHERE VERSION = 97)" in out

target = Path(os.environ.get("V097_OUT") or (MIG / "V097__anomaly_mean_ad_fallback.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
