-- V083: ACTION_QUEUE.PERIOD -- the time basis of the ESTIMATED_USD figure
-- (Decision Studio review, finding #7).
--
-- ACTION_QUEUE.ESTIMATED_USD carried no time basis, so estimates authored on
-- different clocks were summed and compared as if identical. The two app-side
-- writers illustrate the mismatch: the AI-chargeback queue insert stores a
-- 30-day (monthly-equivalent) PROJECTED_30D_USD, while the Action Center create
-- form stores whatever dollar figure the operator typed -- a one-time saving, a
-- monthly run-rate, nobody could tell. The Workbench "Estimated opportunity" KPI
-- and the Decision Studio scenario projection then added those apples and oranges
-- into one number.
--
-- This adds a nullable PERIOD label (MONTHLY / ANNUAL / ONE_TIME; NULL =
-- unspecified). No Snowflake procedure writes ESTIMATED_USD, so the migration is
-- a pure additive column -- the app stamps PERIOD at both writers (AI chargeback =
-- MONTHLY; the create form takes an operator-chosen basis) and surfaces it in every
-- reader. Existing rows read NULL (unspecified), which the app renders and discloses
-- as such rather than silently folding into a labeled total.
--
-- Additive nullable column, like V060's TOTAL_ELAPSED_SEC and V082's COMPANY.
-- ACTION_QUEUE is operator data (not a rebuildable), so teardown needs no new entry:
-- a full rebuild never drops it, and ADD COLUMN IF NOT EXISTS is a no-op on re-apply.
--
-- APPLY BEFORE DEPLOYING THE APP: the action-queue readers (action_center,
-- action_queue) SELECT PERIOD by name, so they error against ACTION_QUEUE until this
-- column exists -- apply V083 in Snowsight after V082, then deploy. Owner-applied;
-- this file never runs from the app.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20083, 'V083 requires V082 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 82) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- The time basis of ESTIMATED_USD. Nullable: pre-existing rows and any writer that
-- omits it read NULL (unspecified). The app validates writes to a small vocabulary
-- (MONTHLY / ANNUAL / ONE_TIME); the width leaves room without inviting free text.
ALTER TABLE DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE
    ADD COLUMN IF NOT EXISTS PERIOD VARCHAR(20);

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 83 AS VERSION,
       'ACTION_QUEUE gains PERIOD (VARCHAR(20)): the time basis of the ESTIMATED_USD figure (MONTHLY / ANNUAL / ONE_TIME; NULL = unspecified). The app stamps it at both writers (AI chargeback 30-day projections = MONTHLY, the Action Center create form takes an operator-chosen basis) and surfaces it in every reader so authored estimates on different time bases are no longer summed or compared as if identical. Additive nullable column; no procedure writes it. Apply before deploying the app -- the action-queue readers SELECT PERIOD by name.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 83);
