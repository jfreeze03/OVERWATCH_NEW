-- Versioned migration: SALES.sales_create_schema
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : creates the SALES subject-area schema that the SALES tables, views, procedure and task live in.
--          IF NOT EXISTS: an adopted (baselined) runway may already have it, and the statement must be a no-op
--          there. The deploy role runs it, so the deploy role owns the schema (bootstrap gives it CREATE SCHEMA on
--          the runway database). Never CREATE OR REPLACE SCHEMA: that drops every object in it (guard FG006X).
-- Rules  : docs/CONVENTIONS.md. Schema-qualify every object (SCHEMA.OBJECT); never name the database.

CREATE SCHEMA IF NOT EXISTS SALES
    COMMENT = 'Sales subject area: orders and the daily order summary';
