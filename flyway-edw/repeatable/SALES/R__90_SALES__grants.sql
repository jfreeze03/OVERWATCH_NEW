-- Repeatable (layer 90: grants): SALES schema access for the runway's functional roles
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : grants are desired state: GRANT is idempotent, so Flyway can re-run this file whenever it (or the
--          role_prefix placeholder) changes. Layer 90 runs after every object it names has been (re)created.
--          role_prefix is EDW_<runway> (EDW_DEV ... EDW_PRD), so each runway grants to its own roles from the same
--          text. The roles are account objects created by a Snowflake admin (bootstrap/ or the role-provisioning
--          process), never by a migration (guard FG007); a missing role fails this file on that runway.
--          USAGE on the runway DATABASE is a database-level grant and belongs in bootstrap/ (guard FG007).
--          ALL ... grants cover the objects that exist now; FUTURE ... grants cover objects created later.
--          Replaced views, functions and procedures keep their explicit grants through COPY GRANTS.
--          Keep future grants for a schema at ONE level (here, schema level): a schema-level future grant takes
--          precedence over a database-level one for the same object type in that schema.
--          Verify on first run (DEV): defining FUTURE grants needs more than object ownership (MANAGE GRANTS, or
--          ownership of a managed-access schema); bootstrap must give the deploy role what it needs.
-- Rules  : docs/CONVENTIONS.md (Grants). Never grant to PUBLIC; never GRANT OWNERSHIP in a migration (FG007).

-- Readers (reporting): the views are the contract, so readers get views, not tables.
GRANT USAGE ON SCHEMA SALES TO ROLE ${role_prefix}_SALES_READ;
GRANT SELECT ON ALL VIEWS IN SCHEMA SALES TO ROLE ${role_prefix}_SALES_READ;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA SALES TO ROLE ${role_prefix}_SALES_READ;

-- Writers (the sales ETL): table DML, plus running the summary refresh on demand.
GRANT USAGE ON SCHEMA SALES TO ROLE ${role_prefix}_SALES_WRITE;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA SALES TO ROLE ${role_prefix}_SALES_WRITE;
GRANT SELECT, INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA SALES TO ROLE ${role_prefix}_SALES_WRITE;
GRANT USAGE ON PROCEDURE SALES.SP_REFRESH_ORDER_SUMMARY() TO ROLE ${role_prefix}_SALES_WRITE;
