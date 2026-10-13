-- Undo migration: SALES.sales_create_orders
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : reverses V2026.10.13.0900__sales_create_orders.sql with `flyway undo` (Enterprise). Flyway undoes only
--          the LATEST applied version, so this runs only while 2026.10.13.0900 is the newest version on the runway
--          (before 2026.10.13.0915 adds ORDER_STATUS). It drops the table and its rows: use it on DEV / test runways
--          for a create that has not been promoted; on MGM / PRD roll back by fixing forward (docs/RUNBOOKS.md).
--          IF EXISTS keeps the undo safe to re-run after a partial failure (Snowflake DDL autocommits).
-- Rules  : docs/CONVENTIONS.md. The waiver below is the guard's ticketed exception for this one DROP TABLE.
-- FLYWAY-WAIVER: FG006 EDW-0000 undo of the create in the same version

DROP TABLE IF EXISTS SALES.ORDERS;
