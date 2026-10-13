-- Repeatable (layer 90: grants): SALES schema access for the runway's reader role
-- Ticket : EDW-0000          Author: EDW platform team (kit sample)
-- Why    : grants are desired state: GRANT is idempotent, so Flyway can re-run this file whenever it (or the
--          role_prefix placeholder) changes. Layer 90 runs after every object it names has been (re)created.
--          role_prefix is EDW_<runway> (EDW_DEV ... EDW_PRD), so each runway grants to its own role from the same
--          text. The role is an account object created by a Snowflake admin (bootstrap/ or the role-provisioning
--          process), never by a migration (guard FG007); a missing role fails this file on that runway.
--          USAGE on the runway DATABASE is a database-level grant and belongs in bootstrap/ (guard FG007).
--          The views are the reporting contract, so readers get views, not tables. ALL VIEWS covers the views that
--          exist now, FUTURE VIEWS the ones created later; a replaced view keeps its grants through COPY GRANTS.
--          Keep future grants for a schema at ONE level (here, schema level): a schema-level future grant takes
--          precedence over a database-level one for the same object type in that schema.
--          Verify on first run (DEV): defining FUTURE grants needs more than object ownership (MANAGE GRANTS, or
--          ownership of a managed-access schema); bootstrap must give the deploy role what it needs.
-- Rules  : docs/CONVENTIONS.md (Grants). Never grant to PUBLIC; never GRANT OWNERSHIP in a migration (FG007).

GRANT USAGE ON SCHEMA SALES TO ROLE ${role_prefix}_SALES_READ;
GRANT SELECT ON ALL VIEWS IN SCHEMA SALES TO ROLE ${role_prefix}_SALES_READ;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA SALES TO ROLE ${role_prefix}_SALES_READ;

-- Lets readers call the shared net-amount rule in their own queries. Querying SALES.V_OPEN_ORDERS does not need it:
-- a view uses its owner's privileges on the objects it references.
GRANT USAGE ON FUNCTION SALES.FN_NET_AMOUNT(NUMBER, NUMBER) TO ROLE ${role_prefix}_SALES_READ;
