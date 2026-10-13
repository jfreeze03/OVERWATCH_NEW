# Flyway shortcomings for the ALFA EDW, and what the kit does about each

The owner asked to "list any known shortcomings of Flyway and create the necessary rules or scripts or processes to
alleviate". This is that list. Each entry gives the shortcoming with its evidence, what it means for the six
`ALFA_EDW_*` runways, the kit artefact that mitigates it, and the risk that remains. The summary table at the end
gives one line per entry.

## How to read this

**Evidence.** Flyway behaviour was read from the Flyway 13.10.0 source and docs (repository commit `b4251cd`,
2026-10-08). The kit pins 13.8.1 (`runways.toml [kit] flyway_version`). The cited behaviours are long-standing, but
re-check the lines whenever `flyway_version` changes. Path prefixes:

- `[core]` = `flyway-core/src/main/java/org/flywaydb/core/`
- `[util]` = `flyway-core-utilities/src/main/java/org/flywaydb/core/utilities/`
- `[sf]` = `flyway-database/flyway-database-snowflake/src/main/java/org/flywaydb/database/snowflake/`
- `[cli]` = `flyway-commandline/src/main/`
- `[docs]` = `documentation/Reference/`

Snowflake, Jenkins and Bitbucket evidence is a documentation URL or the plugin source file. Where a point could not be
verified from source, the text says "verify on first run".

**Enforced by.** Each mitigation names the control that enforces it. Four controls are automatic:
- **guard**: `python3 scripts/fw.py guard`, rules FG001-FG020 in `scripts/fwkit/guard.py`. It runs in every PR build
  and every deploy, and an error fails the build.
- **code review**: `flyway check -code -check.code.failOnError=true`, with `rules/ALFA01`-`ALFA06` and
  `sqlfluff.cfg`.
- **pipeline**: a stage in `jenkins/Jenkinsfile` (CI) or `jenkins/Jenkinsfile.deploy` (one deploy job per runway).
- **callback**: `callbacks/*.sql`, which Flyway runs inside every `migrate`.

Two are manual:
- **bootstrap**: `bootstrap/*.sql`, which a Snowflake admin runs deliberately in Snowsight.
- **process**: a human step in `docs/GUIDE.md` or `docs/RUNBOOKS.md`.

The guard is the authoritative gate. It needs no licence, no database and no Python packages, and it sees the git
base branch. `check -code` is a second opinion from Flyway's own engine. Checks that need a database, such as
baseline state, promotion, drift and rehearsal, belong to the deploy job.

---

## 1. Concurrency, transactions and recovery

### S01 Flyway takes no lock on Snowflake

**Shortcoming.** Flyway normally serializes concurrent `migrate` runs by locking its history table. On Snowflake that
lock does nothing: `[sf]SnowflakeTable.java:59-62` is `doLock() { // no-op }`, and
`[docs]Database Driver Reference/Snowflake.md:108` lists parallel migrations as unavailable. Two processes migrating
the same database read the same pending list, and both of them execute it.

**Impact for ALFA EDW.** Several clients could run against `ALFA_EDW_DEV` at the same moment: a main-branch DEV deploy,
a branch deploy and a developer's laptop. On PRD, a deploy started while another is still running would apply every
pending V twice. The second run fails partway through, on objects the first run created, and leaves failed history
rows (S02).

**Mitigation.**
- pipeline: `Jenkinsfile.deploy` wraps Pre-flight, Rehearsal, Approval and Migrate in
  `lock(resource: <runways.toml lock>)`. There is one lock per database, from `ALFA_EDW_DEV` to `ALFA_EDW_PRD`.
  `jenkins/casc-snippet.yaml` defines the locks up front, so a misspelt lock name cannot create a new ephemeral lock.
  Every job also sets `disableConcurrentBuilds()`.
- guard: FG019 fails when two runways share a lock, and warns when a lock name differs from its database name.
- bootstrap/process: the MGM and PRD deploy keys exist only in the Jenkins folders `EDW/deploy/MGM` and
  `EDW/deploy/PRD` (GUIDE Part B). No human holds `EDW_MGM_DEPLOYER` or `EDW_PRD_DEPLOYER`. The network policy in
  `bootstrap/10_account_objects.sql` lets `SVC_EDW_FLYWAY_*` users log in only from the Jenkins egress addresses.
- process: every break-glass procedure in RUNBOOKS (R0) first reserves the same lockable resource in Jenkins.

**Residual risk.** The lock only coordinates Jenkins jobs. Anyone with a deployer key and an allowed network address
can still start a second `migrate`, and Snowflake gives Flyway no lock to stop them.

### S02 Snowflake DDL is not transactional, so a failed migration is half-applied

**Shortcoming.** Snowflake commits each DDL statement on its own, and Flyway knows this
(`[sf]SnowflakeDatabase.java:185-187`: `supportsDdlTransactions()` returns false).
- If a V script fails at statement 4, statements 1-3 stay applied.
- Flyway records the script with `"success" = FALSE` and logs "Please restore backups and roll back database and
  code!" (`[core]internal/command/DbMigrate.java:341-357`).
- Every later `migrate` then refuses to run ("contains a failed migration") until that row is removed
  (`[core]internal/command/DbMigrate.java:238-263`).
- Grouping migrations does not help: `group = true` only logs a warning that it is undefined on such databases
  (`[core]internal/command/DbMigrate.java:145-150`).
- Flyway runs each migration inside a transaction (`executeInTransaction` defaults to true;
  `[core]internal/jdbc/ExecutionTemplateFactory.java`). A DML-only script therefore rolls back on failure. Any DDL in
  the same script commits that transaction implicitly.

**Impact for ALFA EDW.** A change can be half-applied on MGM or PRD, and the runway is then blocked for every project
until someone cleans up and runs `repair`.

**Mitigation.**
- process: the rules in docs/CONVENTIONS.md:
  - one logical change per V script;
  - idempotent DDL (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`), so that a re-run after repair converges;
  - DDL and DML never mixed in one data fix.
- guard: FG006 blocks statements that are unsafe to repeat, unless the file waives them with a ticket: DROP TABLE,
  TRUNCATE, CREATE OR REPLACE TABLE, DROP COLUMN, and DELETE without WHERE.
- pipeline:
  - MGM and PRD migrate a zero-copy clone first (Rehearsal stage, using `fw.py rehearsal-sql`).
  - The "Record Time Travel point" stage logs a UTC timestamp before Migrate.
  - The prod-tier dry-run report shows exactly what will execute.
- callback:
  - `beforeMigrate__10_deploy_log` writes a STARTED row to `FLYWAY.DEPLOY_LOG`.
  - `afterMigrateError__99_deploy_log` writes a FAILED row. Its insert sits in its own exception handler, so it never
    hides the original error.
- process: RUNBOOKS R1 covers the recovery: inspect, clean up, `flyway repair`, then re-run or fix forward.

**Residual risk.** A statement that passed rehearsal can still fail on the real target, for example because data
changed after the clone was taken, or because of a privilege or warehouse limit. Recovery is manual. On PRD it needs a
break-glass deployer session.

### S03 Undo cannot undo partial failures, repeatables or data loss

**Shortcoming.** `flyway undo` (Teams/Enterprise) has these limits:
- It runs U scripts for versioned migrations only. Repeatables cannot be undone
  (`[docs]Commands/Undo.md:7, :9-20, :57`).
- Undo assumes the versioned migration succeeded completely, so it does not help after a partial failure on a database
  without DDL transactions (https://documentation.red-gate.com/fd/undo-migrations-273973334.html).
- It cannot recover data that a migration dropped.
- `group` cannot make Snowflake DDL atomic.

**Impact for ALFA EDW.** If a release replaced 20 views and altered 3 tables, no single command rolls it back.

**Mitigation.**
- guard:
  - FG002: every U script needs a V twin with the same version and description. An orphan U is an error.
  - FG006: a destructive U script (DROP TABLE) must carry an explicit waiver line.
- `python3 scripts/fw.py new --schema SALES --desc create_returns --undo` creates the V/U pair.
- process: docs/CONVENTIONS.md "Undo scripts" says when to write one. RUNBOOKS R3 lays out the three rollback tools.
  The default is fix-forward:
  - undo, for a V that completed;
  - the previous repeatable version from git;
  - Time Travel (`bootstrap/40_point_in_time_recovery.sql`).

**Residual risk.**
- Undoing a change whose data was modified afterwards still loses data.
- Rolling back a repeatable means a new commit, which goes through promotion like any other.
- `undo` fires no migrate callbacks, so `FLYWAY.DEPLOY_LOG` does not record it. The change ticket must.

### S04 `repair` is a blunt instrument

**Shortcoming.** `flyway repair` does three things (`[docs]Commands/Repair.md`):
- it removes failed rows;
- it re-aligns the checksum, description and type of every applied versioned migration with the files on disk
  (`[core]internal/command/DbRepair.java:216-227`);
- it marks missing migrations as deleted unless an ignore pattern covers them (`DbRepair.java:181-204`).

Re-aligning checksums hides an edited applied file. A "deleted" mark makes Flyway treat that version as never applied
if the file reappears (`[core]internal/info/MigrationInfoServiceImpl.java:220-223, :355-375`).

**Impact for ALFA EDW.** Running `repair` on MGM or PRD from the wrong checkout, such as a feature branch or an old
bundle, rewrites history. Prod runways ignore only `*:future`, so other projects' applied work could be marked deleted.
An edited script would also be accepted silently.

**Mitigation.**
- guard: FG003 keeps applied files byte-identical to the base branch, so an edited applied file never reaches `main`.
- pipeline: bundles are immutable. `fw.py bundle` records a SHA-256 per file, and `fw.py verify-bundle` refuses a
  bundle that was altered, or that was not built from `main` when the target is prod tier.
- flyway.toml (FG018): nonprod runways ignore `*:missing` and `*:future`, so `repair` leaves other projects' branch
  migrations alone there.
- process: RUNBOOKS R1 and R2 allow `repair` only under these conditions:
  - with `-environment=<ID>`;
  - from the extracted and verified bundle that is deployed on that runway;
  - inside the reserved Jenkins lock;
  - with `flyway info` captured before and after.

**Residual risk.** `repair` is a manual act that needs deployer rights. Nothing stops a person with those rights from
running it in the wrong directory.

---

## 2. Session state and the history table

### S05 Flyway resets ROLE and SCHEMA before each migration, but not DATABASE or other session state

**Shortcoming.** Before every migration Flyway runs `restoreOriginalState()` and `changeCurrentSchemaTo(schema)`
(`[core]internal/command/DbMigrate.java:434-435`). On Snowflake those are `USE ROLE <original>` and
`USE SCHEMA <history schema>` (`[sf]SnowflakeConnection.java:41-48, :71`). It never resets the database, the warehouse
or `ALTER SESSION` parameters.

**Impact for ALFA EDW.** `flyway.toml` sets `defaultSchema = "FLYWAY"`, which has three consequences:
- An unqualified `CREATE TABLE ORDERS` lands in `ALFA_EDW_<ID>.FLYWAY`.
- A `USE DATABASE` in a migration redirects every later migration in the same run to that database, along with
  Flyway's own `SHOW SCHEMAS` checks.
- `USE WAREHOUSE` and `ALTER SESSION SET ...` silently apply to every later migration in the run.

**Mitigation.**
- guard:
  - FG008 rejects `USE DATABASE/SCHEMA/ROLE/SECONDARY ROLES` with an error that cannot be waived. `USE WAREHOUSE` and
    `ALTER SESSION` are errors too, but a file can waive them with a ticket.
  - FG010 requires every target of CREATE/ALTER/DROP/UNDROP/COMMENT ON to be `SCHEMA.OBJECT`.
- code review: `ALFA02__no_use_statement`.
- flyway.toml (FG018): `defaultSchema = "FLYWAY"`, so a missed qualifier lands in one known schema, never PUBLIC.
- process: owner's-rights procedures cannot change session state at all (docs/CONVENTIONS.md, Procedures).

**Residual risk.**
- FG010 cannot see names that are assembled at run time in `EXECUTE IMMEDIATE` strings.
- An object created in FLYWAY by mistake does not show in the drift report, because the drift query ignores the
  FLYWAY schema.

### S06 The history table is quoted lower-case, with fixed column sizes

**Shortcoming.** On Snowflake, Flyway quotes every identifier (`[core]internal/database/base/Database.java:258-268`).
It creates `FLYWAY."flyway_schema_history"` with quoted lower-case columns, including `"version" VARCHAR(50)`,
`"description" VARCHAR(200)`, `"script" VARCHAR(1000)` and `"installed_on" TIMESTAMP_LTZ(9)`
(`[sf]SnowflakeDatabase.java:89-115`).
- Longer descriptions and script paths are truncated silently
  (`[core]internal/schemahistory/SchemaHistory.java:217-219`).
- Repeatables are matched to their history rows by the full description
  (`[core]internal/info/MigrationInfoServiceImpl.java:193-197, :286-297`). A repeatable with a description over 200
  characters would therefore never match its own row, and would re-run on every deploy. This follows from reading the
  code; FG001 makes it moot.
- Flyway warns that changing QUOTED_IDENTIFIERS_IGNORE_CASE after the history table exists causes problems
  (`[sf]SnowflakeDatabase.java:55-58`).

**Impact for ALFA EDW.**
- `SELECT * FROM FLYWAY.FLYWAY_SCHEMA_HISTORY` fails with "does not exist". Every query and grant must write
  `FLYWAY."flyway_schema_history"` and `"success"`.
- A change to QUOTED_IDENTIFIERS_IGNORE_CASE at account level could make the history table unreadable to Flyway.

**Mitigation.**
- guard: FG001 rejects any file whose Flyway description exceeds 200 characters or whose path exceeds 1000.
- bootstrap: `20_runway_database.sql` grants the auditor SELECT on the quoted table name. `fw.py drift-sql` and every
  RUNBOOKS query use the quoted names.
- process: keep `QUOTED_IDENTIFIERS_IGNORE_CASE = FALSE` for the account and for the `SVC_EDW_FLYWAY_*` users. Check it
  with `SHOW PARAMETERS LIKE 'QUOTED_IDENTIFIERS_IGNORE_CASE' IN USER SVC_EDW_FLYWAY_PRD;`. Flyway also logs the value
  at every connect ("QUOTED_IDENTIFIERS_IGNORE_CASE option is ...").

**Residual risk.** Someone outside the EDW team can change the account parameter. The Jenkins log line shows the change.

---

## 3. Versions, checksums and ordering

### S07 Version collisions, numeric comparison and out-of-order merges

**Shortcoming.**
- Flyway compares version parts as numbers and drops trailing zero parts (`[core]api/MigrationVersion.java:240-316`;
  `tokenize` builds BigIntegers). So `2026.10.13.0900` and `2026.10.13.900` are the same version.
- Two files with the same version stop every command.
- Timestamp versions are written on many branches, so merge order differs from version order. Without `outOfOrder`, a
  lower version merged after a higher one is never applied. Its state is "Ignored" and validate fails
  (`[core]internal/info/MigrationInfoImpl.java:261-272`).

**Impact for ALFA EDW.** Two engineers on two branches can pick the same version. Without `outOfOrder`, a migration
merged after a higher-numbered one would be skipped on every runway that had already run the higher one.

**Mitigation.**
- `python3 scripts/fw.py new --schema SALES --desc <what>` names the file
  `V<UTC yyyy.mm.dd.hhmm>[.<n>]__<schema>_<desc>.sql`, and adds `.<n>` if that minute is already taken.
- guard:
  - FG001 checks the exact file name, the `migrations/<SCHEMA>/` location, that the description starts with
    `<schema_lower>_`, and that the version is a real date and time.
  - FG002 finds duplicate versions across all folders using Flyway's numeric comparison, and U scripts without a V.
  - FG004 is an error for a timestamp more than a day in the future, which would push every later migration out of
    order. It is a warning for a new V older than the newest V on the base branch: that V will apply OUT OF ORDER.
- flyway.toml (FG018): `outOfOrder = true` on every runway, so a lower version merged late still runs.
- process: the MGM/PRD approver sees the out-of-order items in the PR build's FG004 warnings and in the Pre-flight
  `flyway info`.

**Residual risk.** With out-of-order application, PRD may apply A after B even though DEV applied B before A.
Reviewers must catch two concurrent branches that depend on each other. Rehearsal on a clone of MGM or PRD exercises
the real order before the real target.

### S08 Checksums reject any edit to an applied file

**Shortcoming.** A versioned checksum is the CRC32 of the raw file, comments and whitespace included
(`[core]internal/resolver/sql/SqlMigrationResolver.java:91-101`). With `validateOnMigrate = true` (flyway.toml), any
changed byte in an applied V fails the next `migrate`. The usual fix, `flyway repair`, re-aligns the stored checksum
and hides the edit (S04).

**Impact for ALFA EDW.** A harmless comment fix in an applied migration turns every runway red. A real edit would leave
runways with different definitions under the same version.

**Mitigation.**
- guard: FG003 runs with `--base origin/<target branch>` in every PR. Every V, U or B file, and its `.sql.conf`, that
  exists on the base branch must be byte-identical. Deleting or renaming one is also an error. FG003 cannot be waived.
- pipeline: each main build makes one bundle, and every deploy verifies it by SHA-256 (`fw.py verify-bundle`) before
  using it. The deploy job never builds from source.
- process: to change an applied migration, write a new one (docs/CONVENTIONS.md). For a test runway that ran an edited
  branch migration, follow RUNBOOKS R2.

**Residual risk.** FG003 only compares with the base branch. A branch build that ran on DEV, SAN, SEA or PHX and was
edited afterwards (allowed before merge) causes a mismatch on that runway (R2).

### S09 Repeatable checksums include placeholder values; versioned checksums do not

**Shortcoming.** For a repeatable with placeholder replacement on, Flyway computes the checksum after replacing the
placeholders. For a versioned migration it uses the raw file
(`[core]internal/resolver/sql/SqlMigrationResolver.java:91-101`). A repeatable whose stored checksum does not match is
Outdated and re-runs (`[core]internal/info/MigrationInfoServiceImpl.java:286-297`).

**Impact for ALFA EDW.**
1. The same repeatable has a different checksum on every runway, so comparing checksums cannot prove a repeatable
   was promoted.
2. Changing a placeholder value in flyway.toml (`etl_wh`, `tasks_enabled`, ...) re-runs every repeatable that uses it
   on that runway at the next deploy.
3. A repeatable that used `deploy_id`, `git_sha`, `build_url`, `approver` or `${flyway:timestamp}` would re-run on
   every deploy.

**Mitigation.**
- pipeline: `fw.py promote-check` judges repeatables by their state on the predecessor (Success, not Outdated, Pending
  or Failed). It judges versioned migrations by state too, and `flyway validate` against the predecessor, run with the
  auditor login, proves their checksums (`scripts/fwkit/promote.py`).
- guard: FG005 requires every `${name}` to be defined for every environment, including RH_MGM and RH_PRD (each
  environment's placeholders table replaces the root one, so "declared" means present in every environment's table).
- process: docs/CONVENTIONS.md, Placeholders:
  - the four pipeline-supplied values and `${flyway:timestamp}` belong in callbacks only;
  - a placeholder value change in flyway.toml is a release item that names which repeatables will re-run.

**Residual risk.** The "callbacks only" rule depends on reviewers: the guard does not forbid `${deploy_id}` in a
repeatable.

### S10 Repeatables run in description order, not dependency order

**Shortcoming.** Repeatables run after all versioned migrations, sorted by Java `String.compareTo` of their
description (`[core]internal/resolver/ResolvedMigrationComparator.java:26-51`). The description is the file name after
`R__`, without `.sql`, with every `_` replaced by a space (`[core]internal/resource/ResourceNameParser.java:106`). The
comparison is ordinal: space, then digits, then upper case, then lower case.

**Impact for ALFA EDW.** Without a naming rule, suppose `R__SALES__v_open_orders.sql` selects from
`SALES.V_ORDER_BASE`, defined in `R__SALES__v_order_base.sql`. "v open orders" sorts before "v order base", so the
dependent view runs first and fails on a fresh database.

**Mitigation.**
- guard: FG001 enforces the name `R__<LL>_<SCHEMA>__<object>.sql` with layer bands. The description becomes
  `30 SALES  v open orders`, so the layer decides the order first. The bands are:

  | Layer | Objects                                |
  |-------|----------------------------------------|
  | 10    | sequences, file formats, stages        |
  | 20    | functions                              |
  | 30    | views                                  |
  | 40    | materialized views and dynamic tables  |
  | 50    | procedures                             |
  | 70    | streams                                |
  | 75    | pipes                                  |
  | 80    | tasks                                  |
  | 85    | alerts                                 |
  | 90    | grants                                 |
- guard: FG013 finds a repeatable that references, by `SCHEMA.OBJECT`, an object created by another repeatable that
  sorts AFTER it. It uses Flyway's exact description comparison. The fix is to bump the dependent file's layer, for
  example from 30 to 31.
- `python3 scripts/fw.py new --schema SALES --repeatable --layer 30 --object v_open_orders` generates a correct name.

**Residual risk.** FG013 sees schema-qualified references only. It misses an unqualified reference, which resolves in
the object's own schema, and a name built at run time. Ordering errors surface on a fresh database. A rehearsal clone
is not fresh.

### S11 Repeatables re-run only when their own text changes, and Flyway does not check idempotency

**Shortcoming.** A repeatable is pending only when its own checksum differs
(`[core]internal/info/MigrationInfoServiceImpl.java:286-297`). Flyway runs whatever the file contains each time.

**Impact for ALFA EDW.**
1. A repeatable that contains an INSERT or an `ALTER TABLE ... ADD COLUMN` repeats it on every edit.
2. Changing a function's signature does not re-create the views that call it.
3. The layer-90 grants file does not re-run when a view is replaced. Explicit grants survive only because of COPY
   GRANTS (S22).

**Mitigation.**
- guard: FG012 limits a repeatable to these statements:
  - `CREATE OR REPLACE`, `CREATE OR ALTER` and `CREATE ... IF NOT EXISTS`;
  - GRANT and REVOKE;
  - COMMENT;
  - `EXECUTE IMMEDIATE` blocks;
  - SELECT, SHOW and DESCRIBE;
  - state changes on tasks, alerts, pipes and dynamic tables (SUSPEND/RESUME/SET/UNSET/REFRESH);
  - SET/UNSET on views, functions, procedures, streams and materialized views.

  Data changes and table DDL belong in a V script.
- process: the docs/CONVENTIONS.md review checklist says that a signature change touches every dependant in the same
  PR, and that a V script drops the old overload.

**Residual risk.** No check looks at whether an `EXECUTE IMMEDIATE` block is idempotent.

---

## 4. Adoption and environment safety

### S12 The baseline/emptiness check looks only at the configured schemas

**Shortcoming.** If there is no history table, `migrate` raises `NON_EMPTY_SCHEMA_WITHOUT_SCHEMA_HISTORY_TABLE` only
when one of the configured `schemas` is non-empty (`[core]Flyway.java:205-233`). On Snowflake, "empty" counts only
tables, views and sequences (`[sf]SnowflakeSchema.java:52-60`). flyway.toml configures `schemas = ["FLYWAY"]`.

**Impact for ALFA EDW.** Were FLYWAY empty, Flyway would treat a populated `ALFA_EDW_PRD`, with 50+ schemas, as empty. The first `migrate`
against a database that was never baselined would create the history table, then run every V and every repeatable
against live objects.

**Mitigation.**
- flyway.toml (FG018): `baselineOnMigrate = false` and `createSchemas = false`.
- bootstrap: `bootstrap/20_runway_database.sql` creates `FLYWAY.DEPLOY_LOG` and `FLYWAY.DEPLOY_TASK_STATE`, so FLYWAY
  is never empty and an un-baselined `migrate` stops with `NON_EMPTY_SCHEMA_WITHOUT_SCHEMA_HISTORY_TABLE` (a
  deliberate safety net, GUIDE B3).
- process: GUIDE Part D runs `flyway -environment=<ID> baseline -baselineVersion=<kit baseline_version>` once on each
  existing database, then checks it with `flyway info` before the first deploy.
- pipeline: the deploy job refuses to migrate unless `flyway info -outputType=json` reports a `schemaVersion`, which
  means the database is baselined. `ALLOW_UNBASELINED=true` is only for a brand-new, empty runway.

**Residual risk.** A person with deployer rights can still run a manual `migrate`. Adding the EDW schemas to `schemas`
would fix the check, but it would also hand those schemas to `clean` and `createSchemas`. The kit does not do that.

### S13 `clean` is incomplete on Snowflake, and dangerous with this configuration

**Shortcoming.** On Snowflake, `clean` drops only views, tables, sequences, stages, user functions and user procedures
(`[sf]SnowflakeSchema.java:74-118`). It leaves tasks, streams, pipes, alerts, file formats and dynamic tables. It also
cleans only the configured schemas, which here is just FLYWAY.

**Impact for ALFA EDW.** `clean` cannot reset a runway. With this configuration it would wipe `FLYWAY` (the history
table and `DEPLOY_LOG`) and leave the EDW objects untouched.

**Mitigation.**
- flyway.toml (FG018): `cleanDisabled = true` in `[flyway]`. FG018 fails if any runway or rehearsal environment
  overrides it to false.
- process: reset a runway with a zero-copy clone (`bootstrap/30_refresh_runway.sql`, RUNBOOKS R8). The rehearsal
  clones are created and dropped by `fw.py rehearsal-sql`.

**Residual risk.** A person with deployer rights can pass `-cleanDisabled=false` on a command line.

### S14 Flyway silently reads configuration the repository does not show

**Shortcoming.** Flyway picks up settings from two places outside flyway.toml:
- In TOML mode it reads every `FLYWAY_*` environment variable it recognises as a setting
  (`[util]configuration/TomlUtils.java:94-120`, via `[core]internal/configuration/ConfigUtils.java` `convertKey`).
  For example, line 194 maps `FLYWAY_BASELINE_VERSION` and line 347 maps `FLYWAY_TARGET`.
- It auto-loads `flyway.toml` and `flyway.user.toml` from `${user.home}`, from the current directory and from the
  working directory (`[core]internal/configuration/ConfigUtils.java:450-464`).

**Impact for ALFA EDW.**
- Any exported `FLYWAY_<SETTING>` name silently reconfigures every Flyway run in that shell or container. A Jenkins
  folder variable or a pipeline helper that used `FLYWAY_TARGET` for "the runway id" would set Flyway's `target`.
- A `flyway.user.toml` on an agent or in someone's home directory can change url, user or locations without appearing
  in the repository.

**Mitigation.**
- tooling: `fw.py env` emits pipeline settings as `EDW_*` (`EDW_TARGET`, `EDW_LOCK`, `EDW_BASELINE_VERSION`, ...).
  The only `FLYWAY_*` names it prints are the connection variables flyway.toml resolves (`FLYWAY_SF_USER`,
  `FLYWAY_SF_ROLE`, `FLYWAY_SF_WAREHOUSE`, `FLYWAY_RH_DB`), none of which is a Flyway setting. A test in the kit's
  suite locks that (`test_env_command_formats`), and the Jenkins folder variable is `EDW_TARGET`.
- pipeline:
  - `JAVA_ARGS=-Duser.home=<workspace>@tmp/flyway-home` gives Flyway an empty home directory on every build.
  - Flyway runs from the extracted bundle. `fw.py verify-bundle` refuses a bundle that contains any file missing from
    its manifest, which covers a planted `flyway.user.toml`.

**Residual risk.** A person who exports their own `FLYWAY_*` variables (for example from an old shell profile) on a
break-glass host. RUNBOOKS R0 starts from a clean shell.

---

## 5. Parsing, placeholders and script configuration

### S15 Hazards in the Snowflake parser

**Shortcoming.** Flyway's statement splitter for Snowflake has several traps (`[sf]SnowflakeParser.java`):
- `:66-74`: a statement that starts with `DECLARE` is read as one block. The keyword is matched case-insensitively.
- `:97-108`: the block reader then looks for upper-case `DECLARE` and `BEGIN` case-sensitively. A lower-case `declare`
  falls into the `$$` branch instead and reads up to the next `$$`, or to the end of the file.
- `:125-127`: BEGIN counts as a transaction start only when exactly one space and TRANSACTION, WORK or NAME follow it,
  or when the delimiter follows it directly. `BEGIN\nTRANSACTION` and `BEGIN  TRANSACTION` open a block instead.
- `:130-137`: BEGIN, IF, FOR and CASE change the block depth.

**Impact for ALFA EDW.** A script that is split wrongly runs the wrong statements, or swallows the rest of the file into
one statement, on whichever runway runs it first.

**Mitigation.**
- guard: FG015 catches two things:
  - a top-level DECLARE or BEGIN block (one-line `BEGIN TRANSACTION` and `BEGIN WORK` are allowed);
  - a file that ends inside an unterminated string, identifier, comment or `$$`.

  FG015 checks callbacks too.
- process: docs/CONVENTIONS.md requires every Snowflake Scripting block to be `EXECUTE IMMEDIATE $$ ... $$;` and every
  procedure body to be `$$ ... $$`.
- pipeline: for prod tier, `flyway check -dryrun` archives the exact SQL Flyway will send, and rehearsal executes it on
  a clone.

**Residual risk.** The guard's scanner (`scripts/fwkit/sqlscan.py`) is not Flyway's parser. Flyway alone handles
unusual constructs outside `$$`, such as nested CASE or IF in plain SQL. A wrong split would show up on DEV or in
rehearsal.

### S16 Placeholder syntax collides with JavaScript, and an unresolved placeholder fails only at deploy time

**Shortcoming.** Flyway replaces `${name}` everywhere in the file, inside `$$` bodies too. It throws "No value
provided for placeholder" for an unknown name (`[core]internal/parser/PlaceholderReplacingReader.java:201`), and it
matches names case-insensitively (`:58-68`). JavaScript template literals (`${x}`) use the same syntax.

**Impact for ALFA EDW.**
- A JavaScript UDF or procedure that uses a template literal fails its first deploy, on DEV after the merge.
- A JavaScript variable named like a kit placeholder, such as `${env}`, would be silently replaced with "DEV" or "PRD".

**Mitigation.**
- guard: FG005 requires every `${name}` in a migration or callback to be defined for every environment, including
  the rehearsal environments (an environment's table replaces the root one; FG018 checks the per-deploy defaults). It skips a file whose `.sql.conf` sets
  `placeholderReplacement=false`.
- process: docs/CONVENTIONS.md requires JavaScript bodies to get a `<file>.sql.conf` with
  `placeholderReplacement=false` (FG020 checks the value) and to avoid kit placeholder names.

**Residual risk.** The guard passes a JavaScript literal that uses a declared placeholder name in a file without the
`.conf`. A reviewer must catch it.

### S17 Script configuration (`.sql.conf`) is unforgiving, and `shouldExecute` has side effects

**Shortcoming.**
- Flyway reads exactly four keys, and they are case-sensitive: `shouldExecute`, `placeholderReplacement`,
  `executeInTransaction` and `encoding`. Any other key is rejected, and every command that resolves migrations then
  fails (`[core]internal/sqlscript/SqlScriptMetadata.java:38-41, :49-71`; a plain HashMap).
- A migration skipped by `shouldExecute` shows state "Ignored" (`[core]internal/info/MigrationInfoImpl.java:146-148`).
  Validate tolerates that (`:261-267`), but the state is not "Success".
- During a rehearsal, `${flyway:environment}` is `RH_MGM` or `RH_PRD`, not `MGM` or `PRD`.

**Impact for ALFA EDW.**
- A wrong key stops every deploy on every runway.
- `flyway info` cannot tell a deliberate skip from a migration that never ran: the open-source `InfoOutput` leaves
  `shouldExecuteExpression` null (`[core]api/output/InfoOutput.java:70`), and both show "Ignored".
- A `shouldExecute` that tests `${flyway:environment}` behaves differently in the rehearsal than on the target.

**Mitigation.**
- guard:
  - FG020: an unknown key is an error; `placeholderReplacement` and `executeInTransaction` must be true or false; a
    `shouldExecute` that uses `${flyway:environment}` gets a warning.
  - FG001: a `.conf` without its `.sql` is an error.
- flyway.toml: the rehearsal environments set `env = "MGM"` or `env = "PRD"`, so `${env}` behaves the same in rehearsal
  as on the target.
- process: docs/CONVENTIONS.md, Script configuration:
  - skip as few runways as possible: MGM must behave like PRD, or the rehearsal proves nothing;
  - test data is seeded outside the Flyway chain.
- tooling: `fw.py bundle` records each migration's `shouldExecute` expression in the bundle manifest, and
  `fw.py promote-check` accepts "Ignored" on a predecessor only for a migration that declares one. A migration that is
  "Ignored" for any other reason (for example a lower version that missed `outOfOrder`) still blocks promotion.

**Residual risk.**
- FG020 matches the four key names exactly, as Flyway does, and names the intended key when only the case differs.
- The `shouldExecute` evaluator is proprietary, and whether its comparisons are case-sensitive is undocumented. Test
  in DEV.

### S18 Java 16+ and the Snowflake driver's Arrow result format

**Shortcoming.** On Java 16+ the Snowflake JDBC driver hits a formatting error unless
`JDBC_QUERY_RESULT_FORMAT=JSON` is set (or `--add-opens` is passed)
(`[docs]Database Driver Reference/Snowflake.md:109-111`).

**Impact for ALFA EDW.** The `redgate/flyway` images run a Java 16+ runtime (the current base image is Eclipse
Temurin 25), so without the setting every command fails.

**Mitigation.** guard: FG018 requires `JDBC_QUERY_RESULT_FORMAT=JSON` in every environment url, for the runways and the
rehearsals.

**Residual risk.** None known. The Snowflake CLI, which runs the drift and rehearsal SQL, is Python and is not affected.

---

## 6. Snowflake semantics that Flyway does not model

### S19 Account-level objects are outside Flyway's model

**Shortcoming.** Flyway tracks one database: the history table lives in `<DB>.FLYWAY`, and every runway replays the
same scripts. Warehouses, roles, users, integrations, network and authentication policies, resource monitors and
shares are account objects, which all six runways share.

**Impact for ALFA EDW.** A `CREATE WAREHOUSE` or `GRANT ROLE ... TO USER` in a migration runs first on DEV, but it
changes the account for PRD as well. It then re-runs, or fails, on five more runways. A migration could also grant
itself OWNERSHIP or account-level privileges.

**Mitigation.**
- guard: FG007 rejects these statements unless the file waives them with a ticket:
  - CREATE/ALTER/DROP of a WAREHOUSE, ROLE, USER, DATABASE, INTEGRATION, NETWORK POLICY or RULE, RESOURCE MONITOR,
    SHARE, or AUTHENTICATION, PASSWORD or SESSION POLICY (and similar account objects);
  - ALTER ACCOUNT;
  - GRANT/REVOKE ON ACCOUNT, DATABASE, WAREHOUSE, INTEGRATION or USER;
  - GRANT ROLE ... TO USER;
  - GRANT OWNERSHIP.
- code review: `ALFA06__no_account_level_ddl`, and `ALFA04__no_drop_schema_database`.
- bootstrap: `10_account_objects.sql` and `20_runway_database.sql` own every account-level and database-level object. A
  Snowflake admin runs each section with the role it names.
- process: account changes go through the Snowflake admin change process (GUIDE Part B).

**Residual risk.** The roles that migrations grant to, such as `${role_prefix}_SALES_READ`, must exist before the
deploy. If one is missing, the grants repeatable fails at deploy time.

### S20 Cross-database references break clone safety

**Shortcoming.**
- Inside views, UDFs and owner's-rights procedures, a name that does not mention a database resolves in the object's
  own database (https://docs.snowflake.com/en/sql-reference/name-resolution).
- A clone keeps definitions verbatim, so a 3-part name still points at the source database
  (https://docs.snowflake.com/en/user-guide/object-clone).
- For a pipe, a `schema.table` target in COPY INTO already counts as fully qualified, so a cloned pipe loads the
  SOURCE table (same page).
- A table's sequence default follows the clone only when the table and the sequence are cloned together.

**Impact for ALFA EDW.** A view in `ALFA_EDW_SAN` that names `ALFA_EDW_PRD.SALES.ORDERS` reads production data. After
SAN is refreshed from PRD, every hard-coded `ALFA_EDW_PRD` reference makes the test runway read from production. A
resumed pipe would write into production.

**Mitigation.**
- guard:
  - FG009 rejects any `ALFA_EDW_<anything>` literal in code. `${edw_db}` and `${home_db}` are allowed only to qualify a
    DDL target, never inside a VIEW, FUNCTION, PROCEDURE, TASK, PIPE, DYNAMIC TABLE or ALERT body.
  - FG016 requires a pipe's COPY INTO target to be a bare table name.
  - FG010 requires `SCHEMA.OBJECT` targets, which never name a database.
- code review: `ALFA01__no_hardcoded_edw_db`.
- callback: after each migrate, `afterMigrate__80_cross_env_guard` scans every view, function and procedure definition
  and every task in the runway database. It raises when a definition names an `ALFA_EDW_` database other than
  `${edw_db}` or `${home_db}` (RUNBOOKS R10). Rehearsal runs the same scan on the clone first.
- bootstrap: before cloning, `30_refresh_runway.sql` scans PRD's definitions for `ALFA_EDW_PRD` references.

**Residual risk.**
- The guard cannot see a name built from pieces at run time.
- FG009 does not look for `${flyway:database}`, which also becomes a literal database name inside a body.
- The callback raises only after the DDL has committed (S02).

### S21 Flyway knows nothing about task state

**Shortcoming.**
- `CREATE OR REPLACE TASK` creates a new task, which starts suspended. Replacing a root task cancels its next run.
- The root of a scheduled graph must be suspended before any member changes
  (https://docs.snowflake.com/en/sql-reference/sql/create-task, https://docs.snowflake.com/en/user-guide/tasks-graphs).
- `SYSTEM$TASK_DEPENDENTS_ENABLE` resumes a task and all of its dependents
  (https://docs.snowflake.com/sql-reference/functions/system_task_dependents_enable).
- Cloned tasks arrive suspended.
- `afterMigrate` callbacks run only when migrate succeeds ([docs]Callback Events.md).

**Impact for ALFA EDW.**
- After a deploy, a production task graph can silently stay suspended, and loads stop.
- On test runways, tasks copied from PRD must not start pulling from shared sources.
- A failed deploy leaves its graphs suspended, because the restore callback does not run.

**Mitigation.**
- guard: FG014 requires any file that creates or alters a task to contain the `${tasks_enabled}` gate and either
  `SYSTEM$TASK_DEPENDENTS_ENABLE` or `ALTER TASK ... RESUME`.
- flyway.toml: `tasks_enabled` is "true" on MGM and PRD and "false" on DEV, SAN, SEA, PHX, RH_MGM and RH_PRD. FG018
  requires "false" for the rehearsal environments.
- callbacks:
  - `beforeMigrate__20_task_snapshot` records the started root tasks in `FLYWAY.DEPLOY_TASK_STATE`.
  - `afterMigrate__60_task_restore` re-enables those roots where `tasks_enabled = 'true'`, and suspends started roots
    everywhere else.
- process: docs/CONVENTIONS.md, Tasks:
  - use `CREATE OR ALTER TASK`;
  - suspend the root first;
  - one file per graph, in layer 80;
  - one schema and one owner per graph.

  RUNBOOKS R9 covers the restore after a failed deploy.

**Residual risk.**
- After a FAILED migrate the restore is manual (R9).
- The account-wide limit of 10,000 started tasks covers all six runways together.
- Task code first runs on its schedule in MGM.

### S22 CREATE OR REPLACE drops grants, and BCR-2433 is about to change that

**Shortcoming.**
- Without COPY GRANTS, a replaced view, function or procedure loses its explicit grants. With COPY GRANTS it keeps them,
  but no longer inherits future grants.
- BCR-2433 (2026_08 bundle, pending) makes copying the default and adds IGNORE GRANTS
  (https://docs.snowflake.com/en/release-notes/bcr-bundles/2026_08/bcr-2433).
- Tasks have no COPY GRANTS clause.
- A schema-level future grant overrides database-level future grants of the same type in that schema.

**Impact for ALFA EDW.** Each repeatable deploy would strip SELECT from the reporting roles, until the grants file
happened to re-run. The behaviour also flips once the bundle is enabled.

**Mitigation.**
- guard: FG011 rejects `CREATE OR REPLACE [SECURE] VIEW`, `MATERIALIZED VIEW`, `FUNCTION`, `PROCEDURE`,
  `DYNAMIC TABLE` or `STREAM` without COPY GRANTS.
- code review: `ALFA05__copy_grants_on_replace`, a heuristic warning that acts as a second opinion.
- process: docs/CONVENTIONS.md:
  - write COPY GRANTS explicitly everywhere, which is stable under both behaviours;
  - use `CREATE OR ALTER` where Snowflake supports it (tasks, dynamic tables);
  - keep grants as code in each schema's layer-90 repeatable;
  - put future grants at the schema level only.

**Residual risk.** A replaced alert or file format loses its explicit grants. Schema-level future grants cover it. Check
the account's bundle state with `SELECT SYSTEM$SHOW_ACTIVE_BEHAVIOR_CHANGE_BUNDLES();`.

### S23 Flyway runs destructive SQL without a second look

**Shortcoming.** Flyway executes every statement it is given. Its code review (`check -code`) works at lint level, and
`failOnError` needs Enterprise (`[docs]Commands/Check/Check Code.md`).

**Impact for ALFA EDW.**
- `CREATE OR REPLACE TABLE SALES.ORDERS` empties the table on every runway, drops its Time Travel history and makes its
  streams stale (https://docs.snowflake.com/en/user-guide/streams-intro).
- `DROP SCHEMA` drops the schema on six runways.

**Mitigation.**
- guard: FG006 rejects these statements unless the file carries `-- FLYWAY-WAIVER: FG006 <ticket> <reason>`:
  - DROP TABLE, TRUNCATE and CREATE OR REPLACE TABLE;
  - `ALTER TABLE ... DROP COLUMN`;
  - DELETE without WHERE;
  - DROP SCHEMA.

  DROP DATABASE and CREATE OR REPLACE DATABASE/SCHEMA can never be waived.
- code review: `ALFA03__no_create_or_replace_table`, `ALFA04__no_drop_schema_database`, and the Redgate rules enabled in
  `sqlfluff.cfg`.
- pipeline:
  - MGM and PRD rehearse on a clone first;
  - prod tier gets a dry-run report;
  - a Time Travel point is recorded before Migrate.
- bootstrap: `40_point_in_time_recovery.sql` covers clone AT, SWAP WITH and UNDROP.

**Residual risk.** The guard does not block data-changing DML (UPDATE without WHERE, MERGE); reviewers must. A waived
drop still drops.

---

## 7. Environments, promotion and drift

### S24 Flyway has no Snowflake zero-copy clone provisioner

**Shortcoming.** Flyway's provisioners (clean, backup, create-database, docker, snapshot, Redgate Clone) include no
Snowflake zero-copy clone, and the docs mention none (`[docs]Configuration/Environments Namespace/Environment
Provisioner Setting.md`). Snowflake clones also have limits:
- a clone includes only what the cloning role can see;
- hybrid tables need IGNORE HYBRID TABLES;
- external tables are never cloned;
- pipes on internal stages are not cloned;
- a dangling masking-policy reference makes the clone fail.

**Impact for ALFA EDW.** There is no built-in way to rehearse a release on a copy of MGM or PRD, or to refresh a test
runway.

**Mitigation.**
- `fw.py rehearsal-sql --runway <MGM|PRD> --action create|drop` prints identifier-validated SQL. It refuses any name
  other than `ALFA_EDW_RH_<ID>`, and any name equal to a real runway database.
- pipeline: the deploy job runs that SQL through the Snowflake CLI as `EDW_<ID>_DEPLOYER`. That role owns every
  object, so tasks, pipes and streams are cloned. The job then migrates `-environment=RH_<ID>` and drops the clone in a
  finally/post block.
- guard: FG018 requires the RH_MGM and RH_PRD environments, with db taken from `${env.FLYWAY_RH_DB}`,
  `tasks_enabled = "false"` and clean disabled.
- bootstrap:
  - `10_account_objects.sql` grants CREATE DATABASE to the MGM and PRD deployers for rehearsal clones;
  - `30_refresh_runway.sql` refreshes test runways.
- process: RUNBOOKS R8 (refresh) and R12 (clone left behind).

**Residual risk.** Hybrid tables, external tables and internal-stage pipes are not rehearsed. A clone reflects the
target at the moment it was taken.

### S25 Flyway has no notion of promotion, lanes or approval

**Shortcoming.** Each Flyway environment stands alone: nothing stops `flyway -environment=PRD migrate` from running a
file that never ran on MGM. The Jenkins `input` step has no four-eyes rule, and "Restart from Stage" skips every earlier
stage, an approval stage included (jenkins.io `running-pipelines.adoc:41-48, :65-67`).

**Impact for ALFA EDW.** Without kit controls, a branch build could reach PRD, a bundle could skip MGM, and the person
who started a deploy could approve it.

**Mitigation.**
- guard: FG019 keeps runways.toml consistent:
  - the runways are exactly DEV, SAN, SEA, PHX, MGM and PRD, and each database is `ALFA_EDW_<ID>`;
  - prod tier means lanes `["main"]`, an `approval_group`, `rehearsal = true` and a non-empty `promote_from`;
  - every `promote_from` runway comes earlier in the order;
  - PRD has `four_eyes` and `ticket_required`;
  - locks are unique.
- pipeline: the deploy job enforces the policy in four stages:
  - Fetch bundle: copies the bundle by build number, runs `fw.py verify-bundle --require-branch main` for prod tier,
    and checks the lane.
  - Promotion check: runs `flyway info -outputType=json` and `flyway validate` on each predecessor with the auditor
    login, then `fw.py promote-check`, which passes only if at least one predecessor has every migration in the bundle.
  - Approval: an `input` inside the locked stage, with `submitter` set to the `approval_group`. Four-eyes compares the
    approver with the user who started the build.
  - `disableRestartFromStage()` closes the restart route.
- `jenkins/casc-snippet.yaml`: role-strategy lets only the release groups build `EDW/deploy/MGM` and `EDW/deploy/PRD`.

**Residual risk.** Jenkins administrators can always answer an `input`. The four-eyes check reads the build causes,
which may need script approval (verify on first run).

### S26 Drift, change reports and snapshots are PREVIEW on Snowflake

**Shortcoming.**
- Redgate lists Snowflake's comparison-based features as an Enterprise preview
  (https://documentation.red-gate.com/fd/advanced-capabilities-for-snowflake-354615431.html).
- The local reference docs still list only SQL Server, Oracle, PostgreSQL and MySQL as supported for comparisons
  (`[docs]Exit codes and error codes/Comparison error codes.md:91-96`).
- Snowflake snapshot history arrived in 13.8.0.
- For diff and prepare, the comparison scope is the configured `schemas`
  (`[docs]Configuration/Environments Namespace/Environment Schemas Setting.md`), which here is only FLYWAY.
- `checkDriftOnMigrate` is deprecated in 13.10.0.

**Impact for ALFA EDW.** Flyway alone cannot be the PRD drift gate. An out-of-band change, such as a fix typed in
Snowsight, would be overwritten or contradicted silently.

**Mitigation.**
- pipeline: the Pre-flight stage runs the kit's own drift check, which does not depend on preview features:
  - `fw.py drift-sql --runway <ID>` queries INFORMATION_SCHEMA for tables and views whose LAST_DDL, and functions and
    procedures whose LAST_ALTERED, is newer than the last successful migration;
  - `fw.py drift-eval --runway <ID> --rows <json>` applies the runways.toml `[drift]` ignore lists and allowed users,
    then the runway's `drift_gate`: warn, or fail unless `--ack` / `ACK_DRIFT` is set.
- runways.toml: `save_snapshot` and `preview_checks` stay false until the spike in GUIDE Part G passes. After that the
  job adds `-migrate.saveSnapshot=true` and an advisory `flyway check -drift`, which is never the only gate. The spike
  must also show what the preview compares while `schemas` is `["FLYWAY"]`.
- process: MGM and PRD move `drift_gate` from warn to fail after two clean releases. RUNBOOKS R5 covers handling drift.

**Residual risk.** These limits come from `scripts/fwkit/drift.py`:
- Tasks, streams, pipes and alerts expose no last-DDL time in INFORMATION_SCHEMA. Only the preview check covers them.
- Function and procedure rows carry no user, so every such change is reported.
- Only changes since the last successful migration are visible. Once a deploy passes with warn or `ACK_DRIFT`, that
  drift is no longer reported.
- Grant changes are not covered.
- INFORMATION_SCHEMA lists only objects the querying role can see. The query must run as the runway's deployer, which
  owns every object.
- The meaning of LAST_DDL_BY must be confirmed (verify on first run).

### S27 `cherryPick` cannot be set per environment

**Shortcoming.** These settings cannot be overridden per environment: environment, cherryPick, email, token, licenseKey
and undoSqlMigrationPrefix (`[docs]Configuration/Environments Namespace/Environment Flyway Namespace.md:11-19`).
Settings in child namespaces such as `flyway.check` cannot be overridden either (`:21`).

**Impact for ALFA EDW.** A PRD hotfix that must leave unreleased work on main behind cannot be expressed in
flyway.toml. `promote-check` would also block it, because MGM has not run the rest of main.

**Mitigation.**
- process: RUNBOOKS R4. The hotfix merges to main like any other change.
  - If main has nothing else unreleased, it is promoted normally with expedited approvals.
  - Otherwise, a break-glass session runs `flyway -environment=<ID> migrate -cherryPick=<version>` on MGM and then
    PRD. It uses the verified main bundle, rehearses first, and runs inside the reserved Jenkins lock. The rest of main
    follows later; `outOfOrder = true` lets the older versions apply.
- pipeline: the deploy job has no cherry-pick parameter on purpose, and it passes `check.*` options on the command line,
  not per environment.

**Residual risk.** A cherry-pick is a manual, privileged path that bypasses the deploy job's gates. The callbacks record
it in `DEPLOY_LOG`; Jenkins does not.

---

## 8. Licensing, tooling and CI

### S28 A lapsed licence falls back to Community, and proxies need JAVA_ARGS

**Shortcoming.**
- When the permit expires, Flyway "will revert to the Community feature set".
- Licensing failures exit with code 35 (`[docs]Exit codes and error codes.md:12-15`).
- `flyway version -outputType=json` reports `edition` and `permitExpiry`.
- Since 13.9, Flyway ignores the operating system's proxy settings unless `FLYWAY_USE_SYSTEM_PROXIES` is true
  (`[cli]assembly/flyway:46-55`).
- Online licensing calls Redgate at every start-up.

**Impact for ALFA EDW.** Enterprise-only features either error mid-pipeline or silently disappear: undo, cherryPick,
`shouldExecute`, `check -code` failOnError, regex rules and dry-run.

**Mitigation.**
- pipeline:
  - The CI licence stage and the deploy job's Licence check run `flyway version -outputType=json`, and fail unless the
    edition is Enterprise.
  - `FLYWAY_EMAIL` and `FLYWAY_TOKEN` come from the Jenkins credential `flyway-license`. They are never in flyway.toml:
    FG018 rejects email, token and licenseKey keys there.
  - `JAVA_ARGS` carries `-Duser.home=<ws>@tmp/flyway-home` for the licence cache. Behind a proxy it also carries
    `-Dhttps.proxyHost` and `-Dhttps.proxyPort`.
- process:
  - Agents without internet access use an offline permit (`FLYWAY_OFFLINE_PERMIT_PATH`).
  - The licence expiry is checked quarterly (GUIDE Part H).
  - RUNBOOKS R7 covers a lapse.

**Residual risk.** The repository does not document Redgate's licensing hostnames. Confirm the proxy allow-list on the
first run.

### S29 The Flyway Docker image is not a Jenkins agent out of the box

**Shortcoming.**
- `redgate/flyway` inherits `ENTRYPOINT ["flyway"]` (`flyway-docker/dockerfiles/base/Dockerfile:11-18`).
- Jenkins' docker agent starts the container with `cat`, so the container runs `flyway cat` and exits (docker-workflow
  `WithContainerStep.java:199-207`).
- The container runs as the agent's uid, which has no home directory, yet Flyway keeps its licence permit and refresh
  token under `${user.home}`.

**Impact for ALFA EDW.** Every flyway step fails with a confusing "container exited" error, or Flyway cannot write its
licence cache.

**Mitigation.**
- pipeline:
  - The docker agent passes `args '--entrypoint=""'`.
  - `JAVA_ARGS=-Duser.home=<ws>@tmp/flyway-home` sits outside the archived paths, so the refresh token is never
    archived.
  - The image is pinned to `redgate/flyway:<kit flyway_version>`. FG019 requires the tag to equal `flyway_version`, and
    forbids `latest` and x or x.y tags.
- The bundled SQLFluff needs GLIBC 2.35+, and the image provides it.

**Residual risk.** Agents need Docker and access to the image registry. Mirror the image internally.

### S30 Bitbucket keeps no deployment record for Jenkins deploys

**Shortcoming.** Bitbucket Cloud's 2.0 API exposes deployments read-only:
`/repositories/{workspace}/{repo_slug}/deployments` offers only GET (https://api.bitbucket.org/swagger.json). Only
Bitbucket Pipelines can create deployments.

**Impact for ALFA EDW.** The Bitbucket UI cannot show which build is deployed to which runway.

**Mitigation.**
- pipeline:
  - The deploy job posts a commit status with key `deploy-<ID>` on the bundle's git sha, via curl with
    `bitbucket-status-token`.
  - It sets `currentBuild.description` to the target, source build, approver and pending count.
  - It archives `flyway info -outputType=json`.
- callbacks: `FLYWAY.DEPLOY_LOG` in each runway database holds STARTED, SUCCEEDED and FAILED rows with deploy_id,
  git_sha, build_url and approver. The Flyway history table is a second record.

**Residual risk.**
- A status belongs to one commit and key, so a re-deploy of the same commit overwrites it.
- A FAILED `deploy-<ID>` status on a main commit may count against a later PR whose source head is that commit.

### S31 Secrets: Flyway accepts them in plain config, and Jenkins interpolation leaks them

**Shortcoming.**
- Flyway reads password, token and email from TOML and from environment variables.
- Groovy string interpolation in `sh` steps exposes secrets and lets parameters inject shell commands
  (workflow-cps `DSL.java:107-108, :395-404`, UNSAFE_GROOVY_INTERPOLATION).
- A PR's Jenkinsfile and migrations run with every credential its folder can see
  (jenkins.io `securing-org-folders-and-multibranch-pipelines.adoc:26-27, :35-39`).

**Impact for ALFA EDW.** A key committed to the repository, or echoed into a build log, exposes a runway. A PR build
that could see MGM or PRD credentials would hand them to every PR author.

**Mitigation.**
- guard:
  - FG017 scans every kit file for PEM private-key headers, AWS access-key ids and `PASSWORD = '...'` literals. That
    covers `.sql`, `.toml`, `.conf`, `.json`, `.yaml`, `.sh`, `.groovy`, `.cfg`, `.properties` and the Jenkinsfiles.
  - FG018 requires url, user, role, warehouse and `private_key_file` to come from `${env.*}` resolvers, and rejects
    password, token, email and licenseKey keys.
- pipeline:
  - Authentication is key-pair only, with an encrypted PKCS#8 key and a passphrase.
  - Credentials are folder-scoped: the only deploy key in `EDW/deploy/<ID>` is `sf-flyway-<ID>`.
  - PR builds use only the read-only auditor, against DEV.
  - Every `sh` step is single-quoted, which the kit's test suite locks.
  - The controller runs with `-Dorg.jenkinsci.plugins.workflow.cps.DSL.UNSAFE_GROOVY_INTERPOLATION=fail`
    (`jenkins/casc-snippet.yaml` notes).
- bootstrap: `10_account_objects.sql` creates TYPE=SERVICE users, an authentication policy (KEYPAIR, DRIVERS) and a
  network policy limited to the Jenkins egress addresses.
- process: RUNBOOKS R11 covers key rotation.

**Residual risk.** PR authors can read whatever the auditor can, which is the history tables. FG017 does not scan
Markdown files.

### S32 Code review is lint-level and executes no SQL

**Shortcoming.**
- `check -code` reviews files with SQLFluff and regex rules.
- `failOnError`, regex rules and Redgate rules need Enterprise.
- `majorRules` and `majorTolerance` were removed in 12.0.
- Without a URL, the scope defaults to ALL.
- The bundled SQLFluff needs GLIBC 2.35+.

Sources: `[docs]Commands/Check/Check Code.md` and `Code Review Rules.md`.

**Impact for ALFA EDW.** Nothing executes a PR's SQL before merge. SQLFluff's Snowflake dialect does not know the house
rules: clone safety, task gating and layer order.

**Mitigation.**
- guard: the authoritative gate is FG001-FG020, which needs no licence, no database and no packages (stdlib Python).
- pipeline:
  - `flyway check -code -check.code.failOnError=true -check.scope=ALL` with `rules/ALFA01`-`ALFA06` gives a second
    opinion.
  - PR builds run `flyway info` and `flyway validate` against DEV with the auditor, which catches checksum and ordering
    problems against the real history.
- process: before merging a risky change, deploy the branch to DEV through the branch lane (GUIDE F1).

**Residual risk.** DEV is the first place a PR's SQL executes. The regex rules are line-based heuristics.

### S33 Callbacks are not versioned, and `afterMigrate` runs only after success

**Shortcoming.**
- Callbacks get no history row and no checksum. Every `migrate` runs whatever the callback files contain at that
  moment.
- `afterMigrate` fires only when migrate succeeds ([docs]Callback Events.md).
- `afterMigrateError` fires only when a migration fails. An `afterMigrate` callback that fails fires nothing
  (`[core]internal/command/DbMigrate.java:102-124`).
- Callbacks for one event run in description order (`[core]internal/callback/SqlScriptCallbackFactory.java:107,
  :159-171`), which is why the kit numbers them 10, 20, 60, 80 and 99.

**Impact for ALFA EDW.**
- A change to a callback takes effect on every runway at its next deploy, without promotion.
- A failing callback fails the whole deploy after the migrations have already committed.
- When `afterMigrate__80_cross_env_guard` raises, `FLYWAY.DEPLOY_LOG` gets neither a SUCCEEDED nor a FAILED row.

**Mitigation.**
- guard: FG005, FG015 and FG017 check callbacks. FG001 checks their event names.
- process: `callbacks/` is platform code owned by `@alfa/edw-platform` (`bitbucket/CODEOWNERS`).
  `afterMigrateError__99_deploy_log` puts its insert in its own exception handler. RUNBOOKS R9 and R10 cover the
  failure cases.

**Residual risk.** A callback bug shows up first on DEV and then everywhere. For MGM and PRD, rehearsal runs the
callbacks before the real target.

---

## Known gaps in the kit itself

These were found while writing this catalogue. Each one is small, and each has a process workaround until the proper
fix lands.

None open. The four gaps found while writing this catalogue were closed in the same release:
`fw.py env` emits `EDW_*` keys (S14), FG020 matches `.sql.conf` keys case-sensitively (S17), FG009 flags
`${flyway:database}` inside an object body (S20), and FG005 rejects per-deploy placeholders (`deploy_id`, `git_sha`,
`build_url`, `approver`, `flyway:timestamp`) in repeatables, whose checksum would change on every deploy (S09).

## Summary

| ID | Shortcoming | Mitigation | Enforced by | Residual |
|----|-------------|------------|-------------|----------|
| S01 | No lock on Snowflake | `lock()` per DB; keys only in deploy folders | pipeline, FG019 | manual CLI runs |
| S02 | DDL not transactional | small idempotent V; rehearsal; R1 | FG006, pipeline, callback | manual recovery |
| S03 | Undo is narrow | U/V pairs; fix forward; Time Travel | FG002, FG006, process | later data changes |
| S04 | `repair` is blunt | immutable bundle; repair per R1/R2 | FG003, FG018, process | wrong-directory repair |
| S05 | DATABASE/session not reset | no USE; SCHEMA.OBJECT targets | FG008, FG010, ALFA02 | dynamic SQL |
| S06 | Quoted history table | length checks; quoted names | FG001, bootstrap/20 | parameter change |
| S07 | Version collisions, order | `fw.py new`; duplicate check; outOfOrder | FG001, FG002, FG004 | cross-branch deps |
| S08 | Checksum strictness | immutable applied files; verified bundle | FG003, pipeline | edited branch builds |
| S09 | Placeholders in R checksums | promote by state; declared names | FG005, promote-check | review-only rule |
| S10 | Repeatable order | layer bands; dependency order check | FG001, FG013 | unqualified refs |
| S11 | No idempotency check | allowed statements in repeatables | FG012, review | logic in blocks |
| S12 | Emptiness check on `schemas` | explicit baseline; schemaVersion gate | FG018, pipeline | manual migrate |
| S13 | `clean` incomplete | cleanDisabled everywhere; clone refresh | FG018, bootstrap/30 | CLI override |
| S14 | Hidden configuration | unset collisions; private user.home | process, pipeline | kit variable names |
| S15 | Parser hazards | EXECUTE IMMEDIATE $$; dry run; rehearsal | FG015, pipeline | scanner is not parser |
| S16 | `${` vs JavaScript | declared placeholders; `.conf` opt-out | FG005, FG020 | declared name in JS |
| S17 | `.sql.conf`, shouldExecute | key checks; `${env}`; no MGM skip | FG020, FG001, process | key case |
| S18 | Java 16+ Arrow | JDBC_QUERY_RESULT_FORMAT=JSON | FG018 | none known |
| S19 | Account objects | no account DDL; bootstrap/10, 20 | FG007, ALFA06 | roles must exist |
| S20 | Cross-DB refs, clones | no literals; bare pipe target; scan | FG009, FG016, callback | run-time names |
| S21 | Task state | tasks_enabled gate; snapshot/restore | FG014, callbacks | restore after failure |
| S22 | Grants lost on replace | COPY GRANTS; grants as code | FG011, ALFA05 | alerts, file formats |
| S23 | Destructive SQL | waivers only; rehearsal; Time Travel | FG006, ALFA03, ALFA04 | DML |
| S24 | No clone provisioner | `rehearsal-sql`; refresh worksheet | pipeline, FG018 | hybrid/external |
| S25 | No promotion model | promote-check; input; four-eyes | FG019, pipeline | Jenkins admins |
| S26 | Drift is preview | INFORMATION_SCHEMA drift gate | pipeline, process | tasks, pipes, grants |
| S27 | cherryPick not per env | break-glass cherry-pick (R4) | process | outside job gates |
| S28 | Licence lapse, proxy | edition check; permit; JAVA_ARGS | pipeline, FG018 | licensing hosts |
| S29 | Docker ENTRYPOINT | `--entrypoint=""`; pinned image | pipeline, FG019 | registry access |
| S30 | No Bitbucket deploy record | `deploy-<ID>` status; DEPLOY_LOG | pipeline, callbacks | status overwrite |
| S31 | Secrets | secret scan; env resolvers; folder creds | FG017, FG018, pipeline | auditor visibility |
| S32 | Lint-level code review | guard first; validate against DEV | guard, pipeline | DEV runs SQL first |
| S33 | Callbacks unversioned | CODEOWNERS; error-safe logging | FG005, FG015, process | callback bugs |
