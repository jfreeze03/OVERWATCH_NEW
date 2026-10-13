-- Versioned migration: FINANCE.finance_create_schema
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : creates the FINANCE subject-area schema so the FINANCE tables have a home on every runway.
--          IF NOT EXISTS: an adopted (baselined) runway may already have it, and the statement must be a no-op
--          there. The deploy role runs it, so the deploy role owns the schema (bootstrap gives it CREATE SCHEMA on
--          the runway database). WITH MANAGED ACCESS lets the owning deploy role define the layer-90 FUTURE grants
--          (guard FG011). Never CREATE OR REPLACE SCHEMA: that drops every object in it (guard FG006).
-- Rules  : docs/CONVENTIONS.md. Schema-qualify every object (SCHEMA.OBJECT); never name the database.

CREATE SCHEMA IF NOT EXISTS FINANCE
    WITH MANAGED ACCESS
    COMMENT = 'Finance subject area: general-ledger reference data';
