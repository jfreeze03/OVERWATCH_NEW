# Authoring conventions for EDW migrations

These are the rules for writing SQL that Flyway deploys to the six runways (`ALFA_EDW_DEV`, `ALFA_EDW_SAN`,
`ALFA_EDW_SEA`, `ALFA_EDW_PHX`, `ALFA_EDW_MGM`, `ALFA_EDW_PRD`). One file has to behave the same way on all six, on the
rehearsal clones (`ALFA_EDW_RH_MGM`, `ALFA_EDW_RH_PRD`), and on any runway refreshed from PRD.

Where a rule is enforced, the guard rule id (FGnnn, `scripts/fwkit/guard.py`) or the code-review rule id
(`rules/ALFA0n`) is shown. Rules marked **review** have no automatic check, so reviewers must catch them.
docs/SHORTCOMINGS.md explains the Flyway or Snowflake behaviour behind each rule.

Before you open a PR, run:

```bash
python3 scripts/fw.py guard --base origin/main
```

## The ten rules that matter most

1. Never name a database. Write `SCHEMA.OBJECT` everywhere. A database name in code breaks every clone and every
   promotion (FG009, FG010, ALFA01).
2. Tables and data change only in versioned `V` scripts. Never `CREATE OR REPLACE TABLE` (FG006, ALFA03).
3. Views, functions, procedures, streams, dynamic tables, tasks, alerts and grants live in repeatable `R__` files, as
   idempotent definitions (FG012).
4. Every `CREATE OR REPLACE` of a view, function, procedure, materialized view, dynamic table or stream says
   `COPY GRANTS` (FG011, ALFA05).
5. Wrap every Snowflake Scripting block as `EXECUTE IMMEDIATE $$ ... $$;` (FG015).
6. No `USE` statements and no `ALTER SESSION` (FG008, ALFA02).
7. Never change, rename or delete a V/U file once it is on `main`. Write a new migration instead (FG003).
8. A file that creates or alters a task also contains the `${tasks_enabled}` gate and the resume (FG014).
9. Keep account-level objects out of migrations: warehouses, roles, users, integrations and policies belong in
   `bootstrap/` (FG007, ALFA06).
10. One logical change per V script. Snowflake DDL autocommits, so a failed script is half-applied (RUNBOOKS R1).

## 1. Where each object type goes

| Object | File | Layer | Statement form | Rules |
|--------|------|-------|----------------|-------|
| Schema | V | - | `CREATE SCHEMA IF NOT EXISTS SALES` | FG006 |
| Table, column, constraint, clustering | V | - | `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE` | FG006, FG010 |
| Data fix, reference data | V | - | idempotent `MERGE`, guarded `UPDATE` | FG006 |
| Sequence, file format, stage | R | 10 | `CREATE ... IF NOT EXISTS`, `CREATE OR REPLACE` | FG010, FG012 |
| Function (UDF, UDTF) | R | 20 | `CREATE OR REPLACE FUNCTION ... COPY GRANTS` | FG009, FG011 |
| View, secure view | R | 30 | `CREATE OR REPLACE [SECURE] VIEW ... COPY GRANTS` | FG009, FG011, FG013 |
| Materialized view, dynamic table | R | 40 | `... COPY GRANTS`, `CREATE OR ALTER DYNAMIC TABLE` | FG009, FG011 |
| Stored procedure | R | 50 | `CREATE OR REPLACE PROCEDURE ... COPY GRANTS` | FG009, FG011 |
| Stream | R | 70 | `CREATE OR REPLACE STREAM ... COPY GRANTS` | FG011 |
| Pipe | R (+ V to change it) | 75 | `CREATE PIPE IF NOT EXISTS`, bare COPY target | FG016 |
| Task graph, one file per graph | R | 80 | `CREATE OR ALTER TASK` + gated resume | FG014 |
| Alert | R | 85 | `CREATE OR REPLACE ALERT` + gated resume | review |
| Grants inside the schema | R | 90 | `GRANT ... TO ROLE ${role_prefix}_<SCHEMA>_<ACCESS>` | FG007 |
| Warehouse, role, user, integration, policy | bootstrap/ | - | run by a Snowflake admin | FG007 |

A file lives in the folder of the schema it changes: `migrations/SALES/`, `repeatable/SALES/`. To create a schema,
write a V script in that schema's folder.

## 2. Naming and versions

Do not hand-type file names. Let the scaffold name the file:

```bash
python3 scripts/fw.py new --schema SALES --desc add_order_channel            # versioned
python3 scripts/fw.py new --schema SALES --desc create_returns --undo        # versioned + undo pair
python3 scripts/fw.py new --schema SALES --repeatable --layer 30 --object v_open_orders
python3 scripts/fw.py new --schema SALES --desc backfill_channel --at 2026-10-20T10:15+00:00
```

`--at` takes an ISO timestamp. A time with no offset is read as the machine's local time, then converted to UTC.

- **Versioned**: `migrations/<SCHEMA>/V<YYYY>.<MM>.<DD>.<HHMM>[.<N>]__<schema_lower>_<desc>.sql`, for example
  `migrations/SALES/V2026.10.20.1015__sales_add_order_channel.sql`.
- **Undo**: `U<same version>__<same desc>.sql`, next to its V, for example
  `U2026.10.20.1015__sales_add_order_channel.sql`.
- **Repeatable**: `repeatable/<SCHEMA>/R__<LL>_<SCHEMA>__<object_lower>.sql`, for example
  `repeatable/SALES/R__30_SALES__v_open_orders.sql`.
- **Script config**: `<file>.sql.conf`, next to its script, for example
  `V2026.10.20.1015__sales_add_order_channel.sql.conf`.

- The version is a UTC timestamp. Flyway compares version parts as numbers, so `.0900` and `.900` collide (FG002).
  The scaffold adds `.1`, `.2` ... when that minute is already taken in the schema folder. FG002 catches collisions
  across folders and across branches.
- A timestamp more than a day in the future is an error (FG004). A new V older than the newest V on `main` is a
  warning (FG004), because it will apply OUT OF ORDER. Make sure it does not depend on ordering.
- Repeatables run in order of their Flyway description, which is the file name after `R__` with `_` turned into
  spaces: `30 SALES  v open orders`. The comparison is ordinal: space, then digits, then upper case, then lower case.
  The layer number goes first, so it decides the order. If a repeatable uses an object from another repeatable that
  sorts after it, FG013 fails it. Bump the dependent file to the next number in its band (30 to 31).
- A description may be at most 200 characters, and a path at most 1000 (FG001). Those are the history table's column
  sizes.

## 3. Header template

`fw.py new` writes this header. Fill it in. Do not edit it after the file is on `main`: comments are part of a V
checksum.

```sql
-- Versioned migration: SALES.sales_add_order_channel
-- Ticket : EDW-1234          Author: Jane Doe
-- Why    : Orders need the sales channel for the channel-mix report.
-- Rules  : docs/CONVENTIONS.md. Schema-qualify every object (SCHEMA.OBJECT); never name the database;
--          wrap Snowflake Scripting in EXECUTE IMMEDIATE $$ ... $$; run `python3 scripts/fw.py guard` before the PR.
```

## 4. Placeholders

Flyway replaces `${name}` everywhere in a file before running it, inside `$$` bodies too. Names match
case-insensitively. Every `${name}` must be declared for every environment in flyway.toml, or FG005 fails the file.

| Placeholder | DEV value | Meaning and use |
|-------------|-----------|-----------------|
| `${env}` | `DEV` | Runway id; rehearsals use the target's id (`MGM`/`PRD`). Use it in `shouldExecute` |
| `${edw_db}` | `ALFA_EDW_DEV` | Database being migrated (`ALFA_EDW_RH_<ID>` in rehearsal). DDL target only |
| `${home_db}` | `ALFA_EDW_DEV` | The runway's own database, also during rehearsal. Callbacks only |
| `${etl_wh}` | `WH_EDW_ETL_DEV` | Warehouse for tasks, alerts and dynamic tables |
| `${role_prefix}` | `EDW_DEV` | Prefix of the runway's access roles: `${role_prefix}_SALES_READ` |
| `${tasks_enabled}` | `false` | `true` on MGM/PRD. Gates task/alert resumes; pauses pipes and dynamic tables |
| `${deploy_id}` | `manual` | Set by the pipeline on each deploy. **Callbacks only** |
| `${git_sha}`, `${build_url}`, `${approver}` | `unknown`, `manual` | Set by the pipeline. **Callbacks only** |
| `${flyway:environment}` | `DEV` | `RH_MGM`/`RH_PRD` during rehearsal: use `${env}` instead |
| `${flyway:timestamp}` | per run | **Callbacks only** |
| `${flyway:database}` | `ALFA_EDW_DEV` | Never inside an object body (FG009) |

Rules:
- **Repeatables re-run whenever a placeholder value changes.** Flyway checksums a repeatable after replacing its
  placeholders. A repeatable that used `${deploy_id}` or `${flyway:timestamp}` would therefore re-run on every deploy
  (FG005 rejects the per-deploy placeholders in `repeatable/`). Changing a value in flyway.toml, such as `etl_wh` for SAN, re-runs every repeatable that uses it on
  that runway. Say so in the PR.
- **`${edw_db}` may only qualify a DDL target, and you almost never need it.** Flyway's session database is already
  the runway. FG009 fails `${edw_db}` or `${home_db}` inside a VIEW, FUNCTION, PROCEDURE, TASK, PIPE, DYNAMIC TABLE or
  ALERT body: there it becomes a literal database name, and a clone would keep pointing at the original.
- **Placeholders in versioned scripts are fine.** A V checksum is computed on the raw file, so it is identical on
  every runway.

```sql
-- Correct: the session database is already the runway; schema-qualify only.
CREATE OR REPLACE VIEW SALES.V_ORDER_COUNTS COPY GRANTS AS
SELECT o.ORDER_STATUS, COUNT(*) AS ORDER_COUNT FROM SALES.ORDERS o GROUP BY o.ORDER_STATUS;

-- Incorrect (FG009): becomes ALFA_EDW_DEV.SALES.ORDERS on DEV; a refreshed SAN would read DEV.
CREATE OR REPLACE VIEW SALES.V_ORDER_COUNTS COPY GRANTS AS
SELECT o.ORDER_STATUS, COUNT(*) AS ORDER_COUNT FROM ${edw_db}.SALES.ORDERS o GROUP BY o.ORDER_STATUS;
```

### Cross-database references

- **Another EDW runway.** Never reference one. FG009 rejects any `ALFA_EDW_` literal, and the
  `afterMigrate__80_cross_env_guard` callback fails the deploy when any stored definition names another runway.
- **A database with the same name everywhere** (for example `SNOWFLAKE`, or a share mounted under one name). Write it
  directly: `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`.
- **A non-EDW database whose name differs per runway** (for example a reference database `REF_DEV` / `REF_PRD`). Add a
  placeholder to flyway.toml for every environment, including `RH_MGM` and `RH_PRD`, and use it. That body is then
  deliberately pinned to one database: clones of the runway keep pointing at the same reference database.

```sql
-- flyway.toml: [environments.DEV.flyway.placeholders] ref_db = "REF_DEV"  (and one per environment)
CREATE OR REPLACE VIEW SALES.V_ORDERS_WITH_REGION COPY GRANTS AS
SELECT o.ORDER_ID, r.REGION_NAME
FROM SALES.ORDERS o
JOIN ${ref_db}.GEO.REGIONS r ON r.REGION_ID = o.REGION_ID;
```

## 5. Object types

### 5.1 Schemas

```sql
-- migrations/SALES/V2026.10.13.0855__sales_create_schema.sql
CREATE SCHEMA IF NOT EXISTS SALES;
```

Never use `CREATE OR REPLACE SCHEMA` or `DROP SCHEMA`. The first can never be waived (FG006). The second needs a
ticketed waiver (FG006, ALFA04).

### 5.2 Tables (V scripts only)

```sql
-- Correct
CREATE TABLE IF NOT EXISTS SALES.ORDER_RETURNS (
    RETURN_ID    NUMBER(38, 0)  NOT NULL,
    ORDER_ID     NUMBER(38, 0)  NOT NULL,
    RETURNED_AT  TIMESTAMP_LTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    REASON_CODE  VARCHAR(20),
    CONSTRAINT PK_ORDER_RETURNS PRIMARY KEY (RETURN_ID)
);

ALTER TABLE SALES.ORDERS ADD COLUMN IF NOT EXISTS ORDER_CHANNEL VARCHAR(20);
```

```sql
-- Incorrect
CREATE OR REPLACE TABLE SALES.ORDERS (ORDER_ID NUMBER);  -- FG006: drops data, Time Travel history, stales streams
CREATE TABLE ORDER_RETURNS (RETURN_ID NUMBER);           -- FG010: lands in the FLYWAY schema
ALTER TABLE SALES.ORDERS DROP COLUMN LEGACY_FLAG;        -- FG006: data loss; needs a ticketed waiver
```

- Make DDL idempotent (`IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`). After a partial failure, the fixed deploy re-runs
  the whole script (RUNBOOKS R1).
- A statement that cannot be repeated, such as `ALTER TABLE ... RENAME COLUMN`, goes alone in its own V script.
- **`CREATE OR ALTER TABLE`** is allowed in a V script only, and only with the complete column list:
  - any column that is not listed is dropped, and its data can be recovered only through Time Travel;
  - on a very wide table the statement can be partially applied, so follow it with a `DESCRIBE` check in the PR
    description;
  - never use it in a repeatable (**review**: the guard cannot tell).

```sql
CREATE OR ALTER TABLE SALES.ORDER_STATUS_CODES (
    STATUS_CODE  VARCHAR(20)   NOT NULL,
    DESCRIPTION  VARCHAR(200),
    IS_TERMINAL  BOOLEAN       DEFAULT FALSE
);
```

- To delete a table or column on purpose, write a V script with a waiver, a ticket and a reason. Put the waived
  statement alone in its own file, because a waiver covers the whole file:

```sql
-- FLYWAY-WAIVER: FG006 EDW-1250 LEGACY_FLAG unused since 2025, approved by the data owner
ALTER TABLE SALES.ORDERS DROP COLUMN IF EXISTS LEGACY_FLAG;
```

- A sequence used as a column DEFAULT must be in the same schema as its table. Only then does a clone of the schema
  or database use the cloned sequence.

### 5.3 Data fixes and reference data (V scripts)

Flyway runs each migration inside a transaction, and Snowflake rolls back a DML-only script that fails. Any DDL in the
same script commits that transaction, so never mix DDL and DML in one data fix. Write every fix so that running it
twice changes nothing.

```sql
-- Correct: MERGE converges no matter how many times it runs.
MERGE INTO SALES.ORDER_STATUS_CODES t
USING (
    SELECT 'OPEN' AS STATUS_CODE, 'Open order' AS DESCRIPTION, FALSE AS IS_TERMINAL
    UNION ALL
    SELECT 'CANCELLED', 'Cancelled by customer', TRUE
) s
ON t.STATUS_CODE = s.STATUS_CODE
WHEN MATCHED THEN UPDATE SET DESCRIPTION = s.DESCRIPTION, IS_TERMINAL = s.IS_TERMINAL
WHEN NOT MATCHED THEN INSERT (STATUS_CODE, DESCRIPTION, IS_TERMINAL)
    VALUES (s.STATUS_CODE, s.DESCRIPTION, s.IS_TERMINAL);

-- Correct: the WHERE clause makes the backfill idempotent.
UPDATE SALES.ORDERS
SET ORDER_CHANNEL = 'WEB'
WHERE ORDER_CHANNEL IS NULL
  AND ORDER_SOURCE = 'ECOM';
```

```sql
-- Incorrect
DELETE FROM SALES.ORDER_STATUS_CODES;                                -- FG006: empties the table on six runways
INSERT INTO SALES.ORDER_STATUS_CODES VALUES ('OPEN', 'Open', FALSE); -- duplicates on re-run after a failure
```

- **Big backfills.** Do not switch warehouses inside a migration: `USE WAREHOUSE` stays in effect for the rest of the
  Flyway run (FG008). Choose one of these instead:
  - before the deploy, ask the Snowflake admin to size `WH_EDW_DEPLOY_<ID>` up for the release window;
  - ship the backfill as a procedure plus a task, and let the task do the work in batches outside the deploy.
- **Many small INSERTs.** Flyway's `batch` setting groups up to 100 DML statements per round-trip. A single set-based
  MERGE is still better.
- **Test data.** Never put test data in the Flyway chain (see section 7). It comes from the PRD clone (bootstrap/30)
  or from a project script run on the leased runway.

### 5.4 Sequences, file formats, stages (layer 10)

```sql
-- repeatable/SALES/R__10_SALES__seq_order_id.sql
CREATE SEQUENCE IF NOT EXISTS SALES.SEQ_ORDER_ID START = 1 INCREMENT = 1;

-- repeatable/SALES/R__10_SALES__ff_orders_csv.sql
CREATE OR REPLACE FILE FORMAT SALES.FF_ORDERS_CSV
    TYPE = CSV
    FIELD_DELIMITER = ','
    SKIP_HEADER = 1
    NULL_IF = ('', 'NULL')
    COMMENT = 'Order extracts from the order management system';

-- repeatable/SALES/R__10_SALES__stg_order_files.sql
CREATE STAGE IF NOT EXISTS SALES.STG_ORDER_FILES
    DIRECTORY = (ENABLE = TRUE)
    COMMENT = 'Internal landing stage for order extracts';
```

```sql
-- Incorrect
CREATE OR REPLACE SEQUENCE SALES.SEQ_ORDER_ID;   -- restarts numbering: duplicate keys
CREATE OR REPLACE STAGE SALES.STG_ORDER_FILES;   -- replacing an internal stage discards its files
```

- File formats have no COPY GRANTS, so a replaced file format gets its access back from schema-level future grants
  (section 5.13).
- An external stage needs a storage integration, which is account-level and lives in bootstrap. Its URL differs per
  runway, so it needs a declared placeholder.

### 5.5 Functions (layer 20)

```sql
-- repeatable/SALES/R__20_SALES__fn_net_amount.sql
CREATE OR REPLACE FUNCTION SALES.FN_NET_AMOUNT(P_GROSS NUMBER(38, 2), P_DISCOUNT NUMBER(38, 2))
    COPY GRANTS
    RETURNS NUMBER(38, 2)
    LANGUAGE SQL
    COMMENT = 'Gross amount minus discount, never below zero'
AS
$$
    GREATEST(P_GROSS - COALESCE(P_DISCOUNT, 0), 0)
$$;
```

```sql
-- Incorrect
CREATE OR REPLACE FUNCTION SALES.FN_NET_AMOUNT(P_GROSS NUMBER(38, 2))   -- FG011: no COPY GRANTS
    RETURNS NUMBER(38, 2) AS $$ P_GROSS $$;
CREATE OR REPLACE FUNCTION SALES.FN_DISCOUNT_RATE() COPY GRANTS RETURNS NUMBER AS
$$ SELECT MAX(RATE) FROM ALFA_EDW_PRD.SALES.DISCOUNTS $$;              -- FG009: names a database
```

- **Signature changes.** `CREATE OR REPLACE` replaces only the same signature. A new argument list creates a second
  overload, and the old one stays. Put the new signature in the R file. In the same PR, add a V script that drops the
  old overload, and touch every view or procedure that calls it (a repeatable re-runs only when its own text changes):

```sql
-- migrations/SALES/V2026.10.21.0900__sales_drop_old_fn_net_amount.sql
DROP FUNCTION IF EXISTS SALES.FN_NET_AMOUNT(NUMBER);
```

- **JavaScript bodies.** A JavaScript template literal looks like a Flyway placeholder, so Flyway would replace it.
  Give the file a `.sql.conf` with `placeholderReplacement=false` (FG020 checks the value). That file then cannot use
  any placeholder, and FG005 skips it. Do not name JavaScript variables after kit placeholders (`env`, `edw_db`, ...).

```sql
-- repeatable/SALES/R__20_SALES__fn_order_label.sql
-- (+ repeatable/SALES/R__20_SALES__fn_order_label.sql.conf containing: placeholderReplacement=false)
CREATE OR REPLACE FUNCTION SALES.FN_ORDER_LABEL(P_ID FLOAT, P_CHANNEL VARCHAR)
    COPY GRANTS
    RETURNS VARCHAR
    LANGUAGE JAVASCRIPT
AS
$$
    return `${P_CHANNEL}-${P_ID}`;
$$;
```

### 5.6 Views and secure views (layer 30)

```sql
-- repeatable/SALES/R__30_SALES__v_open_orders.sql
CREATE OR REPLACE VIEW SALES.V_OPEN_ORDERS
    COPY GRANTS
    COMMENT = 'Orders not yet shipped, with net amount'
AS
SELECT
    o.ORDER_ID,
    o.CUSTOMER_ID,
    o.ORDER_STATUS,
    SALES.FN_NET_AMOUNT(o.GROSS_AMOUNT, o.DISCOUNT_AMOUNT) AS NET_AMOUNT
FROM SALES.ORDERS o
WHERE o.ORDER_STATUS = 'OPEN';

-- a secure view: same shape
CREATE OR REPLACE SECURE VIEW SALES.V_CUSTOMER_OPEN_ORDERS
    COPY GRANTS
AS
SELECT o.CUSTOMER_ID, COUNT(*) AS OPEN_ORDERS
FROM SALES.V_OPEN_ORDERS o
GROUP BY o.CUSTOMER_ID;
```

```sql
-- Incorrect
CREATE OR REPLACE VIEW SALES.V_OPEN_ORDERS AS            -- FG011: no COPY GRANTS: reporting roles lose SELECT
SELECT * FROM SALES.ORDERS;                              -- review: SELECT * freezes the column list at creation
CREATE OR REPLACE VIEW V_OPEN_ORDERS COPY GRANTS AS      -- FG010: lands in the FLYWAY schema
SELECT o.ORDER_ID FROM SALES.ORDERS o;
```

- Schema-qualify every name inside the body, even in the view's own schema. FG013 can check order only for
  `SCHEMA.OBJECT` references, and qualified names stay correct when someone moves the file.
- Never write `SELECT *`. Snowflake stores the expanded column list, so a later column change in the base table breaks
  the view or silently hides the new column.
- A view that reads another view defined in a repeatable that sorts later fails on a fresh database. FG013 catches it;
  bump the layer (30 to 31).

### 5.7 Materialized views and dynamic tables (layer 40)

```sql
-- repeatable/SALES/R__40_SALES__dt_daily_order_totals.sql
CREATE OR ALTER DYNAMIC TABLE SALES.DT_DAILY_ORDER_TOTALS
    TARGET_LAG = '1 hour'
    WAREHOUSE = ${etl_wh}
AS
SELECT
    o.ORDER_DATE,
    COUNT(*) AS ORDER_COUNT,
    SUM(o.GROSS_AMOUNT) AS GROSS_AMOUNT
FROM SALES.ORDERS o
GROUP BY o.ORDER_DATE;

-- Dynamic tables refresh (and spend credits) on their own: keep them paused where tasks are off.
EXECUTE IMMEDIATE $$
BEGIN
    IF ('${tasks_enabled}' = 'false') THEN
        ALTER DYNAMIC TABLE SALES.DT_DAILY_ORDER_TOTALS SUSPEND;
    END IF;
END;
$$;

-- repeatable/SALES/R__40_SALES__mv_order_counts_by_status.sql
CREATE OR REPLACE MATERIALIZED VIEW SALES.MV_ORDER_COUNTS_BY_STATUS
    COPY GRANTS
AS
SELECT o.ORDER_STATUS, COUNT(*) AS ORDER_COUNT
FROM SALES.ORDERS o
GROUP BY o.ORDER_STATUS;
```

```sql
-- Incorrect
CREATE OR REPLACE DYNAMIC TABLE SALES.DT_DAILY_ORDER_TOTALS   -- FG011: no COPY GRANTS; also forces a full refresh
    TARGET_LAG = '1 hour' WAREHOUSE = ${etl_wh}
AS SELECT o.ORDER_DATE, COUNT(*) AS ORDER_COUNT FROM ${edw_db}.SALES.ORDERS o GROUP BY o.ORDER_DATE;  -- FG009
```

- For dynamic tables, prefer `CREATE OR ALTER DYNAMIC TABLE` (GA since Snowflake 10.27). It keeps the table's data
  and grants, and avoids a full reinitialization where the change allows it.
- Cloned dynamic tables arrive suspended, so after a runway refresh you resume them deliberately.
- Materialized views need Snowflake Enterprise Edition, and they bind closely to their base table. Change the base
  table with `ALTER`, never by replacing it.

### 5.8 Procedures (layer 50)

```sql
-- repeatable/SALES/R__50_SALES__sp_refresh_order_summary.sql
CREATE OR REPLACE PROCEDURE SALES.SP_REFRESH_ORDER_SUMMARY()
    COPY GRANTS
    RETURNS VARCHAR
    LANGUAGE SQL
    EXECUTE AS OWNER
AS
$$
BEGIN
    MERGE INTO SALES.ORDER_SUMMARY t
    USING (
        SELECT o.CUSTOMER_ID, COUNT(*) AS OPEN_ORDERS
        FROM SALES.ORDERS o
        WHERE o.ORDER_STATUS = 'OPEN'
        GROUP BY o.CUSTOMER_ID
    ) s
    ON t.CUSTOMER_ID = s.CUSTOMER_ID
    WHEN MATCHED THEN UPDATE SET OPEN_ORDERS = s.OPEN_ORDERS, REFRESHED_AT = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT (CUSTOMER_ID, OPEN_ORDERS, REFRESHED_AT)
        VALUES (s.CUSTOMER_ID, s.OPEN_ORDERS, CURRENT_TIMESTAMP());
    RETURN 'ok';
END;
$$;
```

- **Owner's rights (`EXECUTE AS OWNER`), always.** Names in the body resolve in the procedure's own database, so the
  procedure is clone-safe and promotable. An owner's-rights procedure also cannot change session state. Caller's
  rights inherit the caller's database, schema and secondary roles, and they behave differently under a task, the
  pipeline or a person.
- **The body goes in `$$ ... $$`.** Flyway reads it as one string, so it never splits the procedure (FG015).
- **No 3-part names.** FG009 rejects `ALFA_EDW_` literals even inside the body, and `${edw_db}` is rejected there too.
- **Dynamic SQL is invisible to every check.** Keep it rare:
  - build names only from `SCHEMA.OBJECT` values on an allow-list;
  - bind them with `IDENTIFIER(:var)`;
  - never build a database name.

```sql
-- inside a procedure body (TABLE_SUFFIX is an argument): an allow-listed, schema-qualified name bound with
-- IDENTIFIER. Scripting expressions use the bare variable name; SQL statements use :name.
    LET target_table VARCHAR := 'SALES.' || table_suffix;
    IF (NOT ARRAY_CONTAINS(target_table::VARIANT,
                           ARRAY_CONSTRUCT('SALES.ORDER_SUMMARY', 'SALES.ORDER_RETURNS'))) THEN
        RETURN 'refused: ' || target_table;
    END IF;
    DELETE FROM IDENTIFIER(:target_table) WHERE REFRESHED_AT < DATEADD('day', -30, CURRENT_TIMESTAMP());
```

```sql
-- Incorrect
CREATE OR REPLACE PROCEDURE SALES.SP_X() COPY GRANTS RETURNS VARCHAR LANGUAGE SQL EXECUTE AS CALLER AS
$$
BEGIN
    USE SCHEMA SALES;                                         -- session change inside a procedure
    INSERT INTO ALFA_EDW_PRD.SALES.AUDIT_LOG VALUES (1);      -- FG009: hard-coded database
    RETURN 'ok';
END;
$$;
```

- Signature changes follow the same rules as functions: drop the old overload in a V script.

### 5.9 Streams (layer 70)

```sql
-- repeatable/SALES/R__70_SALES__strm_orders.sql
CREATE OR REPLACE STREAM SALES.STRM_ORDERS
    COPY GRANTS
    ON TABLE SALES.ORDERS
    APPEND_ONLY = FALSE
    COMMENT = 'Changes to SALES.ORDERS for the order-summary task';
```

- Every re-run of this file replaces the stream and restarts its offset, so its consumer loses any changes it has not
  read yet. The file re-runs only when you edit it (or a placeholder it uses changes). Plan each edit with the
  consumer's owner.
- If a table is ever rebuilt (a waived FG006), its streams go stale. Edit the stream's repeatable in the same PR, so
  the stream is recreated in the same deploy.
- A cloned stream cannot read changes that were unconsumed at clone time. A refreshed runway's CDC starts at the clone
  point.

### 5.10 Pipes (layer 75)

```sql
-- repeatable/SALES/R__75_SALES__pipe_order_files.sql
CREATE PIPE IF NOT EXISTS SALES.PIPE_ORDER_FILES
    AUTO_INGEST = FALSE
    COMMENT = 'Loads order extracts from SALES.STG_ORDER_FILES'
AS
COPY INTO ORDERS_LANDING
FROM @SALES.STG_ORDER_FILES
FILE_FORMAT = (FORMAT_NAME = 'SALES.FF_ORDERS_CSV');

-- Pipes load on their own: keep them paused where tasks are off.
EXECUTE IMMEDIATE $$
BEGIN
    IF ('${tasks_enabled}' = 'false') THEN
        ALTER PIPE SALES.PIPE_ORDER_FILES SET PIPE_EXECUTION_PAUSED = TRUE;
    END IF;
END;
$$;
```

```sql
-- Incorrect
CREATE PIPE IF NOT EXISTS SALES.PIPE_ORDER_FILES AS
COPY INTO SALES.ORDERS_LANDING FROM @SALES.STG_ORDER_FILES;   -- FG016: a cloned pipe would load the SOURCE table
```

- The COPY INTO target is a **bare** table name (FG016). It resolves in the pipe's own schema. Snowflake treats
  `schema.table` as fully qualified: in a cloned runway, that pipe writes into the source database's table.
- **Changing a pipe's COPY statement.** Write a V script with `CREATE OR REPLACE PIPE ...` and make the same edit in
  the R file. The V script recreates the pipe once on each runway. The R file stays `IF NOT EXISTS` for new databases.
  Recreating a pipe drops its load history: never follow it with `ALTER PIPE ... REFRESH` against a stage that still
  holds files the pipe has already loaded.
- Pipes on internal stages are not cloned. Auto-ingest pipes need a notification integration, which is account-level
  and lives in bootstrap.

### 5.11 Tasks (layer 80)

There is one file per task graph, named for the root task. All tasks of a graph live in one schema and have one
owner, the runway's deployer role. Snowflake requires this, and transferring ownership of a single task cuts it out of
its graph.

```sql
-- repeatable/SALES/R__80_SALES__task_refresh_order_summary.sql
-- Graph root: SALES.TASK_REFRESH_ORDER_SUMMARY. Suspend the root before changing any task of the graph.
ALTER TASK IF EXISTS SALES.TASK_REFRESH_ORDER_SUMMARY SUSPEND;

CREATE OR ALTER TASK SALES.TASK_REFRESH_ORDER_SUMMARY
    WAREHOUSE = ${etl_wh}
    SCHEDULE = 'USING CRON 0 6 * * * America/Chicago'
AS
    CALL SALES.SP_REFRESH_ORDER_SUMMARY();

CREATE OR ALTER TASK SALES.TASK_PUBLISH_ORDER_SUMMARY
    WAREHOUSE = ${etl_wh}
    AFTER SALES.TASK_REFRESH_ORDER_SUMMARY
AS
    CALL SALES.SP_PUBLISH_ORDER_SUMMARY();

-- Resume the whole graph only on runways that run tasks (flyway.toml tasks_enabled: MGM and PRD).
EXECUTE IMMEDIATE $$
BEGIN
    IF ('${tasks_enabled}' = 'true') THEN
        SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('SALES.TASK_REFRESH_ORDER_SUMMARY');
    END IF;
END;
$$;
```

```sql
-- Incorrect
CREATE OR REPLACE TASK SALES.TASK_REFRESH_ORDER_SUMMARY     -- new task id, starts suspended, loses its grants
    WAREHOUSE = ${etl_wh} SCHEDULE = '60 MINUTE'
AS CALL SALES.SP_REFRESH_ORDER_SUMMARY();
ALTER TASK SALES.TASK_REFRESH_ORDER_SUMMARY RESUME;         -- FG014: resumes on DEV/SAN/SEA/PHX and in rehearsal
```

- **Use `CREATE OR ALTER TASK`** (GA July 2026). It keeps the task's identity and grants. `CREATE OR REPLACE TASK`
  creates a new task, suspended, and cancels a root's next scheduled run.
- **Suspend the root first.** Members of a started graph cannot be altered.
- **Gate the resume (FG014).** The file must contain `${tasks_enabled}` and `SYSTEM$TASK_DEPENDENTS_ENABLE` (or
  `ALTER TASK ... RESUME`). `SYSTEM$TASK_DEPENDENTS_ENABLE` on the root resumes the whole graph.
- **Keep the task body to a single `CALL`.** Put the logic in an owner's-rights procedure (layer 50). A changed
  procedure then swaps under the running graph without any suspend, and the task file changes only when the schedule
  or graph shape changes.
- **Never `EXECUTE AS USER`.** It brings in the user's default secondary roles and breaks per-runway isolation.
- **Deploys protect running graphs.** The callbacks `beforeMigrate__20_task_snapshot` and
  `afterMigrate__60_task_restore` re-enable roots that were running before a deploy. They run only after a
  **successful** migrate. After a failure, follow RUNBOOKS R9.
- **Started tasks share one account-wide cap** of 10,000 across all six runways, which is one more reason the test
  runways keep tasks off.

### 5.12 Alerts (layer 85)

```sql
-- repeatable/SALES/R__85_SALES__alert_stale_order_summary.sql
CREATE OR REPLACE ALERT SALES.ALERT_STALE_ORDER_SUMMARY
    WAREHOUSE = ${etl_wh}
    SCHEDULE = '60 MINUTE'
    IF (EXISTS (
        SELECT 1
        FROM SALES.ORDER_SUMMARY s
        WHERE s.REFRESHED_AT < DATEADD('hour', -26, CURRENT_TIMESTAMP())
    ))
    THEN CALL SALES.SP_NOTIFY_STALE_ORDER_SUMMARY();

EXECUTE IMMEDIATE $$
BEGIN
    IF ('${tasks_enabled}' = 'true') THEN
        ALTER ALERT SALES.ALERT_STALE_ORDER_SUMMARY RESUME;
    END IF;
END;
$$;
```

- Alerts are created suspended, so resume them behind the same `${tasks_enabled}` gate as tasks. FG014 covers tasks
  only, so this one is **review**.
- A replaced alert loses its explicit grants. Schema-level future grants (section 5.13) restore them.

### 5.13 Grants (layer 90)

There is one grants file per schema: `R__90_<SCHEMA>__grants.sql`. Grants go to the runway's own access roles, which
bootstrap creates, through `${role_prefix}`.

```sql
-- repeatable/SALES/R__90_SALES__grants.sql
GRANT USAGE ON SCHEMA SALES TO ROLE ${role_prefix}_SALES_READ;
GRANT SELECT ON ALL VIEWS IN SCHEMA SALES TO ROLE ${role_prefix}_SALES_READ;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA SALES TO ROLE ${role_prefix}_SALES_READ;
GRANT USAGE ON FUNCTION SALES.FN_NET_AMOUNT(NUMBER, NUMBER) TO ROLE ${role_prefix}_SALES_READ;
GRANT USAGE ON FUTURE FILE FORMATS IN SCHEMA SALES TO ROLE ${role_prefix}_SALES_LOAD;
```

```sql
-- Incorrect
GRANT USAGE ON DATABASE ${edw_db} TO ROLE ${role_prefix}_SALES_READ;    -- FG007: database grants live in bootstrap
GRANT ROLE ${role_prefix}_SALES_READ TO USER JDOE;                     -- FG007: user grants live in bootstrap
GRANT OWNERSHIP ON TASK SALES.TASK_PUBLISH_ORDER_SUMMARY TO ROLE X;    -- FG007; also cuts the task out of its graph
```

**The grant model.**
- Explicit grants travel with each object: COPY GRANTS on every `CREATE OR REPLACE` that supports it.
- Schema-level future grants cover new objects, and replaced objects of types that have no COPY GRANTS (tasks, alerts,
  file formats).
- Define future grants **at the schema level only**. A schema-level future grant silently overrides every
  database-level future grant of the same type in that schema.
- Snowflake's BCR-2433 (2026_08 bundle, pending) makes copying grants the default. Explicit COPY GRANTS behaves the
  same before and after it.
- Verify on first run that a brand-new view created with `COPY GRANTS` still receives the schema's future grants:
  `SHOW GRANTS ON VIEW SALES.V_OPEN_ORDERS;`.
- The grants file re-runs only when its text changes. Edit it when you add a grant, not when you add a view.
- In a managed-access schema, only the schema owner, which is the deployer, can grant. That is why bootstrap makes the
  deployer the owner of every schema.

### 5.14 Undo scripts

`flyway undo` (Enterprise) runs `U<version>__<desc>.sql` to reverse the V with the same version and description
(FG002 checks that the pair matches). Undo runs only by hand (RUNBOOKS R3), never in the pipeline.

Write a U script when the V **creates** objects that nothing depends on yet (a new table, a new schema object). The U
script is then a clean `DROP ... IF EXISTS`. Do not write one, and fix forward instead, for:
- data fixes;
- ALTERs on populated tables;
- anything whose reversal loses data written after the deploy.

Repeatables cannot be undone: re-apply the previous version from git (RUNBOOKS R3).

```sql
-- migrations/SALES/U2026.10.13.0900__sales_create_orders.sql
-- FLYWAY-WAIVER: FG006 EDW-0000 undo of the create in the same version
DROP TABLE IF EXISTS SALES.ORDERS;
```

Undo assumes the V fully succeeded. After a partial failure, follow RUNBOOKS R1, not undo.

## 6. Snowflake Scripting and the Flyway parser

Flyway's Snowflake parser has two traps. A top-level `DECLARE`/`BEGIN` block is split wrongly (and a lower-case
`declare` swallows the rest of the file), and `BEGIN` counts as a transaction only when exactly one space and
`TRANSACTION`/`WORK` follow it on the same line.

```sql
-- Correct: every anonymous block wrapped; Flyway sees one string.
EXECUTE IMMEDIATE $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO :v_count FROM SALES.ORDER_STATUS_CODES;
    IF (v_count = 0) THEN
        INSERT INTO SALES.ORDER_STATUS_CODES (STATUS_CODE, DESCRIPTION) VALUES ('OPEN', 'Open order');
    END IF;
END;
$$;

-- Correct: explicit transaction, written on one line with one space. Rarely needed: Flyway already runs
-- each migration in a transaction.
BEGIN TRANSACTION;
UPDATE SALES.ORDERS SET ORDER_CHANNEL = 'WEB' WHERE ORDER_CHANNEL IS NULL;
COMMIT;
```

```sql
-- Incorrect (FG015)
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO :v_count FROM SALES.ORDER_STATUS_CODES;
END;

BEGIN
TRANSACTION;                          -- split across lines: Flyway opens a block and mis-splits the file
```

- Placeholders inside `$$` are still replaced. That is how `'${tasks_enabled}'` works inside the gates above.
- An odd number of `$$` markers, or a file that ends inside a string or a comment, is an FG015 error.

## 7. Script configuration (`<file>.sql.conf`)

A `.sql.conf` is a properties file next to its script. Flyway accepts exactly four keys, **case-sensitive**, and any
other key breaks every Flyway command on every runway:

| Key                    | Values                       | Use                                                     |
|------------------------|------------------------------|---------------------------------------------------------|
| `shouldExecute`        | expression (Enterprise)      | run a script on some runways only. See the rules below  |
| `placeholderReplacement` | `true` / `false`           | `false` for JavaScript bodies (section 5.5)             |
| `executeInTransaction` | `true` / `false`             | rarely needed on Snowflake                              |
| `encoding`             | e.g. `UTF-8`                 | only for non-UTF-8 files                                |

FG020 checks for unknown keys and true/false values. FG020 compares key names without regard to case, but Flyway does
not, so write them exactly as shown.

```properties
# V2026.10.20.1100__sales_mask_customer_email.sql.conf
# Mask e-mail addresses on every runway except production.
shouldExecute=${env}!=PRD
```

**Rules for `shouldExecute`:**
- **Use `${env}`, never `${flyway:environment}`.** During a rehearsal `${flyway:environment}` is `RH_MGM`/`RH_PRD`,
  but `${env}` is `MGM`/`PRD`. FG020 warns about it.
- **A skipped script has state "Ignored" on that runway.** `fw.py bundle` records the expression in the bundle
  manifest, so `fw.py promote-check` accepts that "Ignored" as delivered; any other "Ignored" still blocks promotion.
- **Skip as few runways as possible.** MGM must behave like PRD, or the rehearsal and the MGM deploy prove nothing
  about PRD. Skipping PRD only (for example a PRD-only grant), or one test runway, is the normal use.
- **Never seed test data through the Flyway chain.** It would have to skip MGM.
- The evaluator is proprietary, and whether its comparisons are case-sensitive is undocumented. Prove a new
  expression on DEV first.

## 8. Waivers

A waiver line suppresses one rule's **errors** for the **whole file**:

```sql
-- FLYWAY-WAIVER: FG006 EDW-1250 LEGACY_FLAG unused since 2025, approved by the data owner
```

- The format is `-- FLYWAY-WAIVER: FGnnn <ticket> <reason>`. A waiver without both a ticket and a reason is an FG001
  error.
- Put the waived statement in its own file, so the waiver cannot hide a second, unintended finding.
- These can never be waived:
  - FG003, applied-file immutability;
  - FG006 for DROP DATABASE and CREATE OR REPLACE DATABASE/SCHEMA;
  - FG008 for USE DATABASE/SCHEMA/ROLE/SECONDARY ROLES;
  - FG017, secrets;
  - the configuration rules FG018 and FG019.
- Typical legitimate waivers:
  - FG006, a planned drop with a data-owner sign-off;
  - FG007, a one-off grant that security approved;
  - FG008, `USE WAREHOUSE` for a backfill (section 5.3 lists better options).

## 9. Review checklist

The author ticks these before asking for review, and the reviewer checks them. The guard covers the starred items;
the rest are judgment.

- [ ] File created with `fw.py new`; the header is filled in (ticket, author, why).*
- [ ] Every object is `SCHEMA.OBJECT`; no database names, no `USE`, no `ALTER SESSION`.*
- [ ] Tables and data change only in V scripts, which are idempotent (`IF NOT EXISTS`, MERGE, guarded UPDATE).
- [ ] One logical change per V script; no DDL and DML mixed in one data fix.
- [ ] No V/U file that is already on `main` was edited, renamed or deleted.*
- [ ] Every `CREATE OR REPLACE` that supports COPY GRANTS has it.*
- [ ] No `SELECT *` in views.
- [ ] Repeatable layer is right; anything this file uses sorts before it.*
- [ ] Signature change: old overload dropped in a V script; every caller touched in the same PR.
- [ ] Task files: root suspended first, `CREATE OR ALTER TASK`, `${tasks_enabled}` gate,
      `SYSTEM$TASK_DEPENDENTS_ENABLE`.*
- [ ] Alerts, pipes and dynamic tables are gated or paused where `${tasks_enabled}` is `false`.
- [ ] Pipe COPY INTO targets are bare table names.*
- [ ] No `${deploy_id}`, `${git_sha}`, `${build_url}`, `${approver}`, `${flyway:timestamp}` or `${flyway:database}`
      in migrations or repeatables.
- [ ] New placeholder: declared for every environment in flyway.toml, including RH_MGM and RH_PRD.*
- [ ] `.sql.conf` keys spelt exactly; `shouldExecute` uses `${env}` and does not skip DEV, MGM or all test runways.
- [ ] Destructive statements carry a waiver with a ticket; an undo script exists where it makes sense.
- [ ] Out-of-order warning (FG004) understood: the change does not depend on migrations merged after it.
- [ ] Grants: new access granted in the schema's layer-90 file; future grants at schema level only.
- [ ] `python3 scripts/fw.py guard --base origin/main` is clean locally; the PR build is green.
