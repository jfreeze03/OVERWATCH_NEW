"""rec#26: data-quality monitors — table row-volume health for registered products.

App-only half: a robust-z row-volume monitor over ACCOUNT_USAGE.TABLE_DML_HISTORY,
scoped to catalog-registered data products so scans stay bounded and every finding
routes to a known owner. Null-rate spike and schema-drift monitors (which need a
stored baseline) and the DQ_BREACH alert are the deferred owner-migration halves.
"""

from __future__ import annotations

from app.config import core_object


def product_row_volume(days: int = 28) -> str:
    """Daily rows-added per registered-product table over the window (excl. today).

    Scoped to ENTITY_CATALOG entries carrying a DATA_PRODUCT: an OBJECT entity
    matches the table's full DB.SCHEMA.TABLE name (case-insensitively), else its
    DATABASE entity matches the database. DATA_PRODUCT / OWNER_NAME / CRITICALITY
    ride along so a flagged table routes to its owner. The robust-z math runs in
    logic/dq.row_volume_anomalies, which scores each table's most recent load
    (rows-added > 0) against its prior loads (no densification — a day with no
    load simply isn't a data point here)."""
    days = max(7, min(int(days or 28), 90))
    return f"""
WITH catalog AS (
    SELECT ENTITY_TYPE, UPPER(ENTITY_KEY) AS K, DATA_PRODUCT, OWNER_NAME, CRITICALITY
    FROM {core_object('ENTITY_CATALOG')}
    WHERE NULLIF(TRIM(DATA_PRODUCT), '') IS NOT NULL
),
object_map AS (
    SELECT K, DATA_PRODUCT, OWNER_NAME, CRITICALITY FROM catalog WHERE ENTITY_TYPE = 'OBJECT'
),
database_map AS (
    SELECT K, DATA_PRODUCT, OWNER_NAME, CRITICALITY FROM catalog WHERE ENTITY_TYPE = 'DATABASE'
),
dml AS (
    SELECT d.DATABASE_NAME,
           d.DATABASE_NAME || '.' || d.SCHEMA_NAME || '.' || d.TABLE_NAME AS FQN,
           DATE(d.START_TIME) AS DAY,
           SUM(d.ROWS_ADDED) AS ROWS_ADDED
    FROM SNOWFLAKE.ACCOUNT_USAGE.TABLE_DML_HISTORY d
    WHERE d.START_TIME >= DATEADD('day', -{days}, CURRENT_DATE())
      AND d.START_TIME < CURRENT_DATE()
      AND UPPER(d.DATABASE_NAME) <> 'SNOWFLAKE'
      -- dated load tables (name_YYYYMMDD_...) truncate-reload and can't hold a
      -- stable baseline; the steady-mover gate in Python drops the rest.
      AND NOT REGEXP_LIKE(d.TABLE_NAME, '.*_[0-9]{{8}}(_[0-9]+)+', 'i')
    GROUP BY 1, 2, 3
)
SELECT m.FQN, m.DAY, m.ROWS_ADDED,
       COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) AS DATA_PRODUCT,
       COALESCE(om.OWNER_NAME, dm.OWNER_NAME) AS OWNER_NAME,
       COALESCE(om.CRITICALITY, dm.CRITICALITY) AS CRITICALITY
FROM dml m
LEFT JOIN object_map om ON om.K = UPPER(m.FQN)
LEFT JOIN database_map dm ON om.K IS NULL AND dm.K = UPPER(m.DATABASE_NAME)
WHERE COALESCE(om.DATA_PRODUCT, dm.DATA_PRODUCT) IS NOT NULL
ORDER BY m.FQN, m.DAY
LIMIT 8000
"""
