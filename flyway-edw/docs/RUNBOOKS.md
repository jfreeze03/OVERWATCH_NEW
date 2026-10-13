# Runbooks: EDW Flyway incidents

Step-by-step procedures for what goes wrong with deploys to the six runways. Each runbook gives **Who**, **When**,
the **Steps** with exact commands, how to **Verify**, and what **not** to do. The examples use PRD or SAN; for another
runway, replace the id and the database name (`ALFA_EDW_<ID>`). Every SQL statement is plain Snowflake SQL for a
Snowsight worksheet or `snow sql`. Run it as the role named in the step.

docs/SHORTCOMINGS.md explains why each situation can happen. docs/GUIDE.md covers normal deploys.

| Id  | Situation                                         | First move                                         |
|-----|---------------------------------------------------|----------------------------------------------------|
| R0  | Any manual Flyway session (shared setup)           | reserve the Jenkins lock, verify the bundle        |
| R1  | Failed migration, partial DDL                      | read the history, list partial changes             |
| R2  | Checksum mismatch on a test runway                 | find the build that applied the old file           |
| R3  | Roll back a release                                | pick undo / previous repeatable / Time Travel      |
| R4  | Hotfix to PRD                                      | normal fast-track, or a break-glass cherry-pick    |
| R5  | Drift found                                        | identify who changed what; accept, revert or ack   |
| R6  | Stuck lock, or a deploy aborted mid-migration      | lock page; running queries; orphan STARTED row     |
| R7  | Licence lapse, exit code 35, proxy                 | `flyway version -outputType=json`                  |
| R8  | Refresh a test runway between projects             | `bootstrap/30_refresh_runway.sql`                  |
| R9  | Task graph left suspended                          | `FLYWAY.DEPLOY_TASK_STATE` for the failed deploy   |
| R10 | Cross-environment guard tripped                    | list definitions naming `ALFA_EDW_*`               |
| R11 | Key rotation                                       | second public-key slot                             |
| R12 | Rehearsal clone left behind                        | `fw.py rehearsal-sql --action drop`                |

---

## R0 Manual Flyway session (shared setup)

**Who.** For DEV: an edw-dba member. For SAN, SEA and PHX: an edw-dba member or the current lease's lead. Each
checks the runway's deployer key out of the enterprise vault (GUIDE B7 vault table). For MGM and PRD: a break-glass
session per GUIDE B7 (an edw-breakglass operator), which needs the release manager's approval and a change ticket.
No human holds the MGM or PRD deployer role day to day.

**When.** Any runbook below that runs `flyway` or SQL as the deployer.

**Where.** On a host the service users' network policy allows. In practice that is a Jenkins agent host, because the
`SVC_EDW_FLYWAY_*` users can log in only from the Jenkins egress addresses (bootstrap/10). The host needs:
- the Flyway CLI at exactly `runways.toml [kit] flyway_version`, or the `redgate/flyway:<that version>` image;
- Snowflake CLI `snowflake-cli==<[kit] snowflake_cli>`;
- Python 3.11+.

**Steps.**

1. **Stop the pipeline.** Open `<JENKINS_URL>/lockable-resources/` and **Reserve** the runway's lock, for example
   `ALFA_EDW_PRD`. While it is reserved, every deploy job for that runway waits. Flyway takes no lock of its own on
   Snowflake (SHORTCOMINGS S01), so this reservation is the only one.
2. **Use the bundle that is deployed.** Download it from the source build: the `edw-flyway-bundle-<build>.tar.gz`
   artifact of `/EDW/ci/edw-flyway/main`, build number in the deploy job's description. Verify it with `scripts/fw.py`
   from a clean checkout of `main`, so the verifier does not come from the bundle it checks. Then work only inside the
   extracted copy:

   ```bash
   python3 scripts/fw.py verify-bundle --bundle edw-flyway-bundle-1234.tar.gz --require-branch main --extract work
   cd work/bundle
   ```

   The `--require-branch main` flag applies to MGM and PRD. Omit it on a test runway that runs branch builds.
3. **Load the runway settings** in a clean shell (no `FLYWAY_*` variables from a profile; SHORTCOMINGS S14):

   ```bash
   eval "$(python3 scripts/fw.py env --runway PRD --format shell)"
   env | grep '^FLYWAY_' | grep -v '^FLYWAY_SF_\|^FLYWAY_RH_DB=' && echo 'unset the FLYWAY_* names above first'
   export SNOWFLAKE_ACCOUNT='ORGNAME-ACCOUNTNAME'  # your account identifier
   export FLYWAY_SF_KEY_FILE="$HOME/secure/svc_edw_flyway_prd.p8"
   read -rs FLYWAY_SF_KEY_PWD && export FLYWAY_SF_KEY_PWD
   read -rs FLYWAY_TOKEN && export FLYWAY_TOKEN && export FLYWAY_EMAIL='edw-flyway@alfa.example'
   # Flyway's licence cache goes to a private home; the BouncyCastle flag lets the driver read the encrypted key.
   export JAVA_ARGS="-Duser.home=$PWD/.flyway-home"
   JAVA_ARGS="$JAVA_ARGS -Dnet.snowflake.jdbc.useBundledBouncyCastleForPrivateKeyDecryption=true"
   ```
4. **Define a helper for plain SQL as the same identity:**

   ```bash
   export PRIVATE_KEY_PASSPHRASE="$FLYWAY_SF_KEY_PWD"
   sfsql() { snow sql --temporary-connection --account "$SNOWFLAKE_ACCOUNT" --user "$FLYWAY_SF_USER" \
       --role "$FLYWAY_SF_ROLE" --warehouse "$FLYWAY_SF_WAREHOUSE" --authenticator SNOWFLAKE_JWT \
       --private-key-file "$FLYWAY_SF_KEY_FILE" "$@"; }
   ```
5. **Check the licence, then capture the starting state.** Save this output in the ticket:

   ```bash
   flyway version -outputType=json      # "edition" must be Enterprise (R7 if not)
   flyway -environment=PRD info
   ```

**When you finish.** Unreserve the lock, delete `work/` (it also holds `.flyway-home`, with the licence token
cache), and close the shell.

**Do not.**
- Do not run Flyway from a git checkout, a feature branch or an old bundle.
- Do not run it with any other `FLYWAY_<SETTING>` variable exported (Flyway reads them as configuration).
- Do not run it while a Jenkins deploy holds the lock.

---

## R1 Failed migration (partial DDL)

**Who.** The DBA on call, plus the author of the failing migration. On MGM or PRD, the release manager approves the
break-glass session.

**When.**
- The deploy job fails in Migrate, and the log shows "Migration ... failed" with the failing statement.
- `FLYWAY.DEPLOY_LOG` has a FAILED row.
- Every later deploy to that runway fails with "contains a failed migration to version ...".

On Snowflake, Flyway runs every statement in autocommit, DDL and DML alike, so every statement before the failing
one has been applied and committed (SHORTCOMINGS S02). Only an explicit `BEGIN TRANSACTION` ... `COMMIT` in the
file is an exception; check whether the failing statement was inside one.

**Steps.**

1. **Do not re-run the job.** Note from the Jenkins log the deploy id (`<ID>-<build>`), the failing version and the
   failing statement.
2. Set up the session (R0).
3. **Read the history.** Run it as the deployer, or as the auditor role `EDW_FLYWAY_AUDITOR`:

   ```sql
   SELECT h."installed_rank", h."version", h."description", h."script", h."installed_by",
          h."installed_on", h."execution_time", h."success"
   FROM ALFA_EDW_PRD.FLYWAY."flyway_schema_history" h
   ORDER BY h."installed_rank" DESC
   LIMIT 10;

   SELECT l.DEPLOY_ID, l.EVENT, l.EVENT_TS, l.GIT_SHA, l.APPROVER, l.DETAIL
   FROM ALFA_EDW_PRD.FLYWAY.DEPLOY_LOG l
   ORDER BY l.EVENT_TS DESC
   LIMIT 10;
   ```
4. **List what the failed run changed.** The drift query returns every table, view, function and procedure whose DDL
   is newer than the last **successful** migration, which includes the partial changes:

   ```bash
   python3 scripts/fw.py drift-sql --runway PRD > drift.sql
   sfsql -f drift.sql
   ```

   Compare the list with the statements of the failing file to see which ones committed.
5. **Decide what kind of failure it is.**
   - (a) **Environmental**: a missing privilege, a suspended or undersized warehouse, a lock timeout, unexpected data.
     The SQL itself is right.
   - (b) **The SQL is wrong**: a syntax or logic error. It will fail again on every runway.
6. **Clean up so a re-run converges.** If the statements are idempotent (`IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`,
   `CREATE OR REPLACE`), there is usually nothing to undo. Otherwise, reverse the statements that committed, by hand,
   as the deployer role, and record the SQL in the ticket. Then check that no query is still running (R6, step 3).
7. **Remove the failed history row.** The runway's ignore patterns protect other projects' rows (SHORTCOMINGS S04):

   ```bash
   flyway -environment=PRD repair
   flyway -environment=PRD info
   ```
8. **Finish.**
   - (a) **Environmental**: fix the cause, unreserve the lock, and re-run the same deploy job with the same
     `SOURCE_BUILD`.
   - (b) **Wrong SQL: fix forward.** The broken file is immutable on `main` (FG003):
     1. Write a NEW V migration with the corrected change, idempotent so it is safe where the bad one partly ran. Get
        it through PR and merge.
     2. On every runway where the broken version is Pending (it never ran there, or `repair` removed its Failed row),
        record it as applied without running it, before that runway's next deploy:

        ```bash
        flyway -environment=PRD migrate -cherryPick=2026.10.20.1015 -skipExecutingMigrations=true \
            -environments.PRD.flyway.placeholders.deploy_id=PRD-SKIP-EDW1234 \
            -environments.PRD.flyway.placeholders.approver="$USER"
        ```

        Pass placeholders in the environment-scoped form shown: an environment's placeholders table replaces the
        root one, so a plain `-placeholders.deploy_id=...` is silently ignored on every runway.

     3. Deploy the fixed build through the pipeline as usual.

     Skipped this way, the broken version shows Success everywhere, so `fw.py promote-check` keeps working. Do not
     add a `.sql.conf` with `shouldExecute=false` instead: it changes the meaning of a migration that already ran on
     some runways, after the fact.

**Verify.**
- `flyway -environment=PRD info` shows no Failed row.
- `flyway -environment=PRD validate` passes.
- The re-run deploy ends with a SUCCEEDED row in `FLYWAY.DEPLOY_LOG`.
- The drift query lists only changes you expect.
- If the migration touched tasks, also follow R9.

**Do not.**
- Do not edit, rename or delete the failed V file. FG003 blocks it, and other runways may have run it.
- Do not delete history rows by hand.
- Do not run `repair` from a different bundle than the one that failed.

---

## R2 Checksum mismatch on a test runway

**Who.** The project leasing the runway, and the runway lead.

**When.** A deploy to DEV, SAN, SEA or PHX fails in Pre-flight validate with "Migration checksum mismatch for
migration version X". It is almost always a branch build that ran on the runway, after which its V file was edited
before merge (allowed until merge; SHORTCOMINGS S08).

**Steps.**

1. Set up the session (R0) with the bundle you want to deploy. On a test runway, omit `--require-branch`.
2. Find the row and the build that applied the old file:

   ```sql
   SELECT h."version", h."description", h."checksum", h."installed_by", h."installed_on"
   FROM ALFA_EDW_SAN.FLYWAY."flyway_schema_history" h
   WHERE h."version" = '2026.10.20.1015';

   SELECT l.DEPLOY_ID, l.EVENT, l.EVENT_TS, l.GIT_SHA, l.BUILD_URL
   FROM ALFA_EDW_SAN.FLYWAY.DEPLOY_LOG l
   WHERE l.EVENT_TS BETWEEN DATEADD('hour', -1, '2026-10-20 10:20:00'::TIMESTAMP_LTZ)
                        AND DATEADD('hour', 1, '2026-10-20 10:20:00'::TIMESTAMP_LTZ)
   ORDER BY l.EVENT_TS;
   ```

   Use the `installed_on` value from the first query. Compare the file at `GIT_SHA` with the new file:
   `git diff <GIT_SHA> -- migrations/SALES/V2026.10.20.1015__sales_add_order_channel.sql`.
3. Choose one option:
   - **A. The edit is cosmetic** (comments, whitespace), or you have applied the difference by hand. Realign the
     stored checksum. The test runways ignore `*:missing` and `*:future`, so `repair` leaves other projects'
     migrations alone:

     ```bash
     flyway -environment=SAN repair
     ```
   - **B. The edit changes behaviour, and a U script existed in the old build.** Undo with the OLD bundle (the one
     that applied it), then deploy the new build:

     ```bash
     flyway -environment=SAN undo -cherryPick=2026.10.20.1015      # run inside the old bundle's directory
     flyway -environment=SAN info                                   # the version shows Undone
     ```
   - **C. Otherwise, or if unsure.** Refresh the runway from PRD (R8), then deploy the project's current build.

**Verify.** `flyway -environment=SAN validate` passes, and the next deploy job for SAN is green.

**Do not.**
- Do not run `repair` when the edit changed behaviour. The runway would keep the old definition under the new
  checksum.
- Once a branch build has run on a shared runway, treat its V files as immutable and add a new V instead.
- A mismatch on **MGM or PRD** should be impossible (FG003, bundle digest). If you see one, stop. Do not repair; open
  an incident and compare the deployed bundle with git.

---

## R3 Roll back a release

**Who.** The release manager decides. The DBA executes; on MGM or PRD that takes a break-glass session (R0).

**When.** A deployed change is wrong and cannot wait for a normal fix-forward.

**Choose the tool:**

| What went wrong | Tool |
|-----------------|------|
| A V that created new objects nobody depends on yet, and a U script exists | `flyway undo` |
| A view, function, procedure, task, alert or grant definition (repeatable) | previous version from git |
| Data was damaged or lost, or a table was altered destructively | Time Travel (bootstrap/40) |
| Anything else | fix forward: a new V or an edited R, through the pipeline |

**Undo** (Enterprise; versioned only; assumes the V fully succeeded).
1. Session (R0).
2. On MGM or PRD, rehearse first on a clone. `fw.py env` already exported `FLYWAY_RH_DB`:

   ```bash
   python3 scripts/fw.py rehearsal-sql --runway PRD --action create > rh_create.sql && sfsql -f rh_create.sql
   flyway -environment=RH_PRD undo -cherryPick=2026.10.20.1015
   python3 scripts/fw.py rehearsal-sql --runway PRD --action drop > rh_drop.sql && sfsql -f rh_drop.sql
   ```
3. Undo on the target:

   ```bash
   flyway -environment=PRD undo -cherryPick=2026.10.20.1015
   flyway -environment=PRD info                     # the version shows Undone
   ```
4. Record it in the change ticket. Undo fires no migrate callbacks, so `FLYWAY.DEPLOY_LOG` does not record it.

An Undone version on MGM blocks PRD promotion of any bundle that still contains it (`fw.py promote-check`), which is
what you want. A later deploy re-applies the V unless main removes its effect with a new migration.

**Previous repeatable from git.**
1. On a branch from `main`, put back the previous content of the file:

   ```bash
   git log --oneline -- repeatable/SALES/R__30_SALES__v_open_orders.sql
   git checkout <good-commit> -- repeatable/SALES/R__30_SALES__v_open_orders.sql
   ```
2. Open a PR, merge, and promote it, with approvals expedited by the release manager (GUIDE F6). The file's checksum
   changes, so Flyway re-runs it on every runway. Because of COPY GRANTS, grants survive.

Do not hand-edit the object in Snowsight: that is drift (R5), and the next change to the file overwrites it.

**Time Travel.**
1. Find the point in time before the deploy, from the "Record Time Travel point" stage in the Jenkins log, or here:

   ```sql
   SELECT l.DEPLOY_ID, l.EVENT, l.EVENT_TS, l.GIT_SHA, l.APPROVER
   FROM ALFA_EDW_PRD.FLYWAY.DEPLOY_LOG l
   WHERE l.DEPLOY_ID = 'PRD-1234'
   ORDER BY l.EVENT_TS;
   ```
2. Follow `bootstrap/40_point_in_time_recovery.sql` as `EDW_PRD_DEPLOYER`. Restore side by side, compare, then swap:

   ```sql
   CREATE TABLE ALFA_EDW_PRD.SALES.ORDERS_RESTORE
       CLONE ALFA_EDW_PRD.SALES.ORDERS AT (TIMESTAMP => '2026-10-20 09:59:00 +00:00'::TIMESTAMP_TZ)
       COPY GRANTS;
   -- compare row counts / key aggregates between the two tables, then:
   ALTER TABLE ALFA_EDW_PRD.SALES.ORDERS SWAP WITH ALFA_EDW_PRD.SALES.ORDERS_RESTORE;
   ```

   A dropped object comes back with `UNDROP TABLE ALFA_EDW_PRD.SALES.ORDERS;`, which works only within the retention
   period and only if no table of that name exists. Streams on a swapped table must be recreated: edit the stream's
   repeatable (CONVENTIONS 5.9).
3. Time Travel does not touch Flyway history. The V still shows Success. Converge with a new idempotent V migration.
   `flyway repair` matters only if the incident also left a Failed row (R1).
4. The next deploy's drift check reports the swapped table. Acknowledge it with `ACK_DRIFT=true`, citing the ticket
   (R5).

**Verify.** The behaviour or data is back. `flyway info` matches what you expect, with no Failed rows. The ticket
records the tool, the time, who did it, and the follow-up migration.

**Do not.**
- Do not undo after a partial failure; that is R1.
- Do not undo a migration whose tables have taken new data you need.
- Do not drop the `*_RESTORE` table until the release manager confirms. Then drop it as a separate, recorded step.

---

## R4 Hotfix to PRD

**Who.** The author writes the fix. The release manager runs the release, and an approver from
`edw-prod-approvers` approves PRD; that must be a different person (four-eyes).

**When.** PRD needs a change before the next regular release.

**Path A: normal fast-track (preferred).** Use it when `main` holds nothing unreleased that must not reach PRD.
1. Branch from `main`, write the fix (`fw.py new`), open a PR, and merge. FG003 and FG004 apply as usual.
2. The main build deploys to DEV automatically. Deploy the same build to one test runway, then MGM, then PRD, with the
   deploy jobs (GUIDE F2-F4). The approvers act immediately. Every gate still runs: promotion check, rehearsal, drift,
   approval.

**Path B: break-glass cherry-pick.** Use it when `main` holds unreleased migrations that must not reach PRD yet.
`cherryPick` cannot be set per environment, and the deploy job deliberately has no cherry-pick parameter
(SHORTCOMINGS S27).
1. Merge the hotfix V to `main` as in path A. Main build N deploys to DEV automatically, with all of `main`.
2. **MGM**: session (R0) with build N's bundle and the MGM lock reserved.
   1. Rehearse:

      ```bash
      python3 scripts/fw.py rehearsal-sql --runway MGM --action create > rh_create.sql && sfsql -f rh_create.sql
      flyway -environment=RH_MGM migrate -cherryPick=2026.10.20.1015
      python3 scripts/fw.py rehearsal-sql --runway MGM --action drop > rh_drop.sql && sfsql -f rh_drop.sql
      ```
   2. Apply on MGM:

      ```bash
      flyway -environment=MGM migrate -cherryPick=2026.10.20.1015 \
          -environments.MGM.flyway.placeholders.deploy_id=MGM-HOTFIX-EDW1234 \
          -environments.MGM.flyway.placeholders.git_sha="$GIT_SHA" \
          -environments.MGM.flyway.placeholders.approver="$APPROVER"
      ```

      Set `GIT_SHA` to the bundle's sha (`bundle-manifest.json`) and `APPROVER` to the approver's id.
3. **PRD**: once the PRD approver has signed the ticket, start a new R0 session for PRD (the PRD key, the
   `ALFA_EDW_PRD` lock, `fw.py env --runway PRD`). Repeat step 2 with `--runway PRD`, `RH_PRD` and
   `-environment=PRD`, and `deploy_id` `PRD-HOTFIX-EDW1234`. Before migrating, record the Time Travel point:

   ```sql
   SELECT CURRENT_TIMESTAMP() AS TIME_TRAVEL_POINT;
   ```
4. Post the Bitbucket status by hand, so the commit shows the deploy. You need a token that may post commit statuses:
   the repository access token behind the Jenkins credential `bitbucket-status-token` if your Bitbucket admin keeps
   a copy in the enterprise vault, or else a short-lived repository access token with Repositories: Read that the
   Bitbucket admin creates for the ticket and revokes afterwards (GUIDE B5). The break-glass operator cannot read a
   credential back out of Jenkins. Set
   `BITBUCKET_WORKSPACE`, `BITBUCKET_REPO` and `CHANGE_URL` (the ticket link) first, and pass the token through a
   private header file, never on the command line, where `ps` would show it:

   ```bash
   read -rsp 'Bitbucket status token: ' BITBUCKET_STATUS_TOKEN && echo
   hdr=$(mktemp) && chmod 600 "$hdr"
   printf 'Authorization: Bearer %s\n' "$BITBUCKET_STATUS_TOKEN" > "$hdr"
   BB_REPO_API="https://api.bitbucket.org/2.0/repositories/$BITBUCKET_WORKSPACE/$BITBUCKET_REPO"
   curl -sS -X POST -H @"$hdr" -H "Content-Type: application/json" \
       "$BB_REPO_API/commit/$GIT_SHA/statuses/build" \
       -d "{\"key\":\"deploy-PRD\",\"state\":\"SUCCESSFUL\",\"name\":\"deploy-PRD (hotfix)\",\"url\":\"$CHANGE_URL\"}"
   rm -f "$hdr"; unset BITBUCKET_STATUS_TOKEN
   ```
5. **Back-merge.** The hotfix is already on `main` in this flow. If someone fixed it on a release branch instead,
   merge it to `main` the same day; FG003 then keeps it immutable. The rest of `main` reaches PRD with the next
   regular promotion. The hotfix version is already applied there, and the older pending versions apply out of order
   (`outOfOrder = true`).

**Verify.**
- `flyway -environment=PRD info` shows the hotfix as Success and the rest of `main` as Pending.
- `FLYWAY.DEPLOY_LOG` on PRD has the `PRD-HOTFIX-...` STARTED and SUCCEEDED rows.
- The next regular PRD deploy passes its promotion check, because MGM ran the same versions.

**Do not.**
- Do not run the cherry-pick on PRD before MGM.
- Do not skip the rehearsal.
- Do not write `cherryPick` into flyway.toml.

---

## R5 Drift found

**Who.** The DBA, and the owner of the drifted object. An approver decides on acknowledging it.

**When.** One of these:
- The deploy job's Pre-flight prints "drift <ID>: N object(s) changed outside Flyway since the last deploy". With
  `drift_gate = "warn"` the job continues; with `"fail"` it stops.
- An advisory `flyway check -drift` report shows drift. That exists only where `preview_checks = true`.

**Steps.**

1. Reproduce the report in an R0 session, as the deployer. INFORMATION_SCHEMA shows only the objects the querying
   role can see, and the auditor role sees almost nothing:

   ```bash
   python3 scripts/fw.py drift-sql --runway PRD > drift.sql
   sfsql -f drift.sql --format json > rows.json
   python3 scripts/fw.py drift-eval --runway PRD --rows rows.json
   ```
2. Find out who changed what and why. ACCOUNT_USAGE lags by up to about 45 minutes. Run as a role with access to the
   SNOWFLAKE database:

   ```sql
   SELECT q.START_TIME, q.USER_NAME, q.ROLE_NAME, q.QUERY_TYPE, LEFT(q.QUERY_TEXT, 200) AS QUERY_TEXT
   FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY q
   WHERE q.DATABASE_NAME = 'ALFA_EDW_PRD'
     AND q.START_TIME >= DATEADD('day', -7, CURRENT_TIMESTAMP())
     AND q.QUERY_TEXT ILIKE '%ORDER_SUMMARY%'
     AND q.QUERY_TYPE NOT IN ('SELECT', 'SHOW', 'DESCRIBE')
   ORDER BY q.START_TIME DESC;
   ```
3. Decide:
   - **Accept** the change; it should be code. Write a V or R migration that reproduces it, idempotently, so it is a
     no-op where the change already exists. Get it through PR and deploy.
   - **Revert** to the code's definition.
     - For a repeatable: make a trivial edit, such as the header's "Why" line. The new checksum makes Flyway re-run
       it on the next deploy.
     - For a table: ALTER it back, or restore it (R3, Time Travel).
     - Where the preview check runs, it writes revert and incorporate scripts into `drift-resolution/`, which the job
       archives. Review them; never run them blindly.
   - **Acknowledge** when the drift is benign or already handled: re-run the deploy with `ACK_DRIFT=true`, and put
     the ticket in `CHANGE_TICKET`.
     - The drift check sees only changes since the last successful migration. Acknowledged drift disappears from the
       next report, so the ticket is the only record.
     - If the same benign change recurs (a loader's scratch table, an ETL service user), add it to `runways.toml`
       `[drift] ignore_name_patterns` or `allowed_ddl_users` through a PR.

**Verify.** `fw.py drift-eval` prints "checked, clean", or lists only the acknowledged items. For an accepted change,
the next deploy applies the new migration cleanly.

**Do not.**
- Do not acknowledge drift you have not explained.
- Do not "fix" PRD by hand to match the code. That is new drift; use a migration.

The drift query cannot see tasks, streams, pipes, alerts or grants (SHORTCOMINGS S26). Check those by hand after any
out-of-band incident: `SHOW TASKS IN SCHEMA ...`, `SHOW GRANTS ON ...`.

---

## R6 Stuck lock, or a deploy aborted mid-migration

**Who.** The Jenkins admin for the lock, and the DBA for the database side.

**When.** One of these:
- A deploy job sits in "waiting for lock".
- A lock stays held after its build ended, for example after a controller restart.
- A deploy was aborted, or its agent died, while Migrate was running.

**Steps.**

1. **Who holds the lock?** Open `<JENKINS_URL>/lockable-resources/`.
   - **Held by a running build that is waiting at Approval.** That is by design: approval sits inside the lock and
     can wait up to 24 hours. Have the approver answer it, or abort that build.
   - **Held by a build that has ended, or reserved by a person who has left.** Use **Unlock** or **Reset** (needs the
     plugin's UNLOCK permission). Unreserve a person's reservation only after asking them.
   - Flyway itself holds no lock on Snowflake (SHORTCOMINGS S01), so there is nothing to clear in the database.
2. **If Migrate was interrupted**, reserve the lock (R0) before touching the database.
3. **Look for statements still running server-side** as the service user, and cancel them:

   ```sql
   SELECT q.QUERY_ID, q.START_TIME, q.EXECUTION_STATUS, LEFT(q.QUERY_TEXT, 120) AS QUERY_TEXT
   FROM TABLE(ALFA_EDW_PRD.INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER(
            USER_NAME => 'SVC_EDW_FLYWAY_PRD', RESULT_LIMIT => 200)) q
   WHERE q.EXECUTION_STATUS IN ('RUNNING', 'QUEUED', 'BLOCKED', 'RESUMING_WAREHOUSE')
   ORDER BY q.START_TIME DESC;

   SELECT SYSTEM$CANCEL_QUERY('01bf2a3c-0000-6b9e-0000-00010d2f5e31');   -- one QUERY_ID from above
   ```

   Alternatively, as the role that owns the user (USERADMIN), stop everything that user runs:
   `ALTER USER SVC_EDW_FLYWAY_PRD ABORT ALL QUERIES;`.
4. **Find the interrupted deploy:** a STARTED row with no SUCCEEDED or FAILED row. The error callback never ran.
   (A cross-environment guard trip or a failed task restore is different: those callbacks write their own FAILED row
   before raising, R9 / R10.)

   ```sql
   SELECT s.DEPLOY_ID, s.EVENT_TS AS STARTED_AT, s.GIT_SHA, s.APPROVER
   FROM ALFA_EDW_PRD.FLYWAY.DEPLOY_LOG s
   WHERE s.EVENT = 'STARTED'
     AND NOT EXISTS (
         SELECT 1 FROM ALFA_EDW_PRD.FLYWAY.DEPLOY_LOG e
         WHERE e.DEPLOY_ID = s.DEPLOY_ID AND e.EVENT IN ('SUCCEEDED', 'FAILED'))
   ORDER BY s.EVENT_TS DESC;
   ```
5. **Check the history (R1, step 3).**
   - If the process died mid-script, the migration may have **no row at all**: it stays Pending, while its first
     statements may have committed.
   - If the migration has a Failed row, follow R1.
   - If it has no row, list the partial changes (R1, step 4). Make sure a re-run converges, cleaning up where it
     would not, and then re-run the deploy.
6. Task graphs suspended by the interrupted run are not restored automatically: follow R9.

**Verify.**
- The lock shows free, or is held by the new build.
- No running queries remain for the service user.
- The re-run deploy writes STARTED and SUCCEEDED rows.

**Do not.**
- Do not force-unlock a resource while its build is still in Migrate.
- Do not start a second migrate "to see what happens".

---

## R7 Licence lapse, exit code 35, or a proxy problem

**Who.** The Jenkins admin, and the owner of the Redgate account.

**When.**
- The CI licence stage or the deploy job's Licence check fails because the edition is not Enterprise.
- Any `flyway` command exits with code 35.
- The log mentions the licence or permit.
- Flyway warns that the permit expires within 30 days.

**Steps.**

1. Read the facts in the same image, with the same environment:

   ```bash
   flyway version -outputType=json | jq -r '.edition, .permitExpiry'
   ```
2. **Expired or revoked personal access token.** The Redgate account owner creates a new token at
   `identityprovider.red-gate.com/personaltokens`. The Jenkins admin then updates the username/password credential
   `flyway-license` in `EDW/ci` and in every `EDW/deploy/<ID>` folder. The credential id stays the same.
3. **The agent cannot reach Redgate licensing.** Flyway 13.9 and later ignores operating-system proxy settings, and
   the JVM never reads `HTTPS_PROXY`. Do not edit the Jenkinsfiles: both build `JAVA_ARGS` themselves and append the
   controller environment variable `EDW_JAVA_PROXY_ARGS`. In Jenkins (Manage Jenkins > System > Global properties,
   or `casc-snippet.yaml` `globalNodeProperties`), set:
   - `EDW_JAVA_PROXY_ARGS` = `-Dhttps.proxyHost=proxy.alfa.example -Dhttps.proxyPort=8080` (your real host and port);
   - `HTTPS_PROXY` and `PIP_INDEX_URL` for the Snowflake CLI install, if those are blocked too;
   - lower-case `http_proxy` / `https_proxy` if the CI Guard stage fails installing git with apt (GUIDE A2).

   Then re-run the job. Only in a manual R0 session, append the same flags to `JAVA_ARGS` after the existing
   `-Duser.home=...` flag:

   ```bash
   JAVA_ARGS="$JAVA_ARGS -Dhttps.proxyHost=proxy.alfa.example -Dhttps.proxyPort=8080"
   ```
4. **No internet access at all.** Use an offline permit:
   1. Download it from `permits.red-gate.com/offline?productCode=63`.
   2. Store it as a Jenkins **Secret file** in the same folders as `flyway-license`.
   3. Bind it with `file(credentialsId: ..., variable: 'FLYWAY_OFFLINE_PERMIT_PATH')`.

   The permit has its own expiry date. Put it in the calendar (GUIDE Part H).
5. Re-run the failed job. The licence check comes before any database step, so a licence failure changed nothing.

**Verify.** The licence stage is green, `edition` is Enterprise, and `permitExpiry` is in the future. Record the next
expiry date in GUIDE Part H's quarterly check.

**Do not.**
- Do not bypass the licence check to "just deploy". In Community edition, undo, cherryPick, `shouldExecute`
  `.sql.conf` files, `check -code` failOnError and the regex rules all fail or silently change behaviour.
- Do not put the token in flyway.toml (FG018).

---

## R8 Refresh a test runway between projects

**Who.**
- A Snowflake admin runs `bootstrap/30_refresh_runway.sql`.
- The runway lead for SAN, SEA or PHX coordinates.
- Data governance signs off, because PRD data is copied into a test runway.

**When.**
- A runway lease passes to the next project.
- A runway has drifted beyond repair.
- R2 option C.

**Steps.**

1. Announce the refresh. The outgoing project must merge, or at least push, its work. Anything that exists only in
   the runway is lost.
2. Reserve the runway's lock, for example `ALFA_EDW_SAN`, in Jenkins (R0, step 1) for the whole refresh. For PART 3
   only (the clone), also reserve `ALFA_EDW_PRD`, so no PRD deploy is half-applied in the clone; unreserve it as soon
   as PART 3 has finished.
3. In Snowsight, run `bootstrap/30_refresh_runway.sql` section by section, with its SET variables pointing at SAN.
   The script:
   - refuses PRD as a target;
   - runs the pre-checks: dangling policy references, hybrid tables, external tables, internal-stage pipes, and
     `ALFA_EDW_PRD` references in PRD definitions;
   - clones `ALFA_EDW_PRD` as `EDW_PRD_DEPLOYER`;
   - hands every schema to `EDW_SAN_DEPLOYER`, re-applies database grants, and re-grants to runway roles;
   - lists the suspended tasks, alerts, dynamic tables and pipes.
4. Check what Flyway now sees, from an R0 session for SAN. The clone brings PRD's history, so SAN is at PRD's state:

   ```bash
   flyway -environment=SAN info
   ```
5. Deploy the incoming project's work with the SAN deploy job: its branch build (lane `branch`) or the latest main
   build. Migrations that are on `main` but not yet on PRD apply now. So do the project's branch migrations.
6. Unreserve the lock. Tell the project three things:
   - tasks stay suspended (`tasks_enabled = "false"`) until they flip it for their runway;
   - streams start at the clone point;
   - the data is production data, under the governance rules.

**Verify.**
- `flyway -environment=SAN info` shows no Failed rows, and the deploy is green.
- `SHOW TASKS IN DATABASE ALFA_EDW_SAN;` shows the tasks suspended.
- The first deploy after the refresh lists the cloned objects as drift: their LAST_DDL is the clone time and
  LAST_DDL_BY the admin who cloned. Review that list once. Where the runway's `drift_gate` is `fail`, re-run the
  deploy with `ACK_DRIFT=true` and the refresh ticket in `CHANGE_TICKET` (R5). The deploy after that is clean.

**Do not.**
- Do not refresh MGM unless the bootstrap script's explicit MGM switch has been approved.
- Do not resume pipes in a refreshed runway that share a stage with PRD.
- Do not run `ALTER PIPE ... REFRESH` there.

---

## R9 Task graph left suspended

**Who.** The DBA on call.

**When.**
- A deploy to MGM or PRD failed or was aborted after it suspended a root task. `afterMigrate__60_task_restore` runs
  only after a **successful** migrate, so it never restored the root.
- Monitoring shows scheduled loads not running.

**Steps.**

1. Find the deploy (R6, step 4, or the latest FAILED row in `FLYWAY.DEPLOY_LOG`). Then list the roots that were
   running before it. `beforeMigrate__20_task_snapshot` recorded them:

   ```sql
   SELECT s.TASK_FQN, s.STATE_BEFORE, s.CAPTURED_TS
   FROM ALFA_EDW_PRD.FLYWAY.DEPLOY_TASK_STATE s
   WHERE s.DEPLOY_ID = 'PRD-1234'
   ORDER BY s.TASK_FQN;
   ```
2. Compare with the root tasks that are suspended now:

   ```sql
   SHOW TASKS IN DATABASE ALFA_EDW_PRD;
   SELECT t."database_name", t."schema_name", t."name", t."state"
   FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) t
   WHERE t."predecessors" = '[]' AND t."state" = 'suspended'
   ORDER BY 1, 2, 3;
   ```
3. Before resuming, make sure the failed migration did not leave that graph half-changed (R1). Keep a half-changed
   graph suspended until the fix-forward is deployed.
4. As `EDW_PRD_DEPLOYER`, the owner of the graph, resume each root and all its dependents. Generate the statements:

   ```sql
   SELECT 'SELECT SYSTEM$TASK_DEPENDENTS_ENABLE(''' || s.TASK_FQN || ''');' AS RESTORE_STMT
   FROM ALFA_EDW_PRD.FLYWAY.DEPLOY_TASK_STATE s
   WHERE s.DEPLOY_ID = 'PRD-1234';
   ```

   Run each generated line. `TASK_FQN` is stored fully quoted, so a line looks like:

   ```sql
   SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('"ALFA_EDW_PRD"."SALES"."TASK_REFRESH_ORDER_SUMMARY"');
   ```
5. On DEV, SAN, SEA, PHX and the rehearsal clones, suspended is the intended state (`tasks_enabled = "false"`). Do
   nothing there unless the project switched tasks on for its runway.

**Verify.** The roots show `started` in `SHOW TASKS`, and the next runs are scheduled:

```sql
SELECT th.NAME, th.STATE, th.SCHEDULED_TIME
FROM TABLE(ALFA_EDW_PRD.INFORMATION_SCHEMA.TASK_HISTORY(
         SCHEDULED_TIME_RANGE_START => DATEADD('hour', -1, CURRENT_TIMESTAMP()),
         RESULT_LIMIT => 100)) th
ORDER BY th.SCHEDULED_TIME DESC;
```

A task that only calls a procedure records a NULL return value, so judge success by the procedure's own output rows,
not by RETURN_VALUE.

**Do not.**
- Do not resume a graph on a test runway that may pull from shared sources.
- Do not transfer ownership of a single task to "fix" it; that cuts it out of its graph.

---

## R10 Cross-environment guard tripped

**Who.** The DBA, and the author of the offending object.

**When.** Migrate fails in `afterMigrate__80_cross_env_guard`, with an error that lists up to 20 objects whose
definitions name another `ALFA_EDW_*` database.
- Every migration of the run has already committed and shows Success.
- Callbacks of one event run in name order, so `afterMigrate__60_task_restore` had already run, and the SUCCEEDED row
  (`afterMigrate__99_deploy_log`) was never written.
- `afterMigrateError` fires only when a migration fails, not when an `afterMigrate` callback fails, so the guard writes
  its own FAILED row (DETAIL starts "Cross-environment reference in") before it raises. The task restore does the
  same when a root does not end in the intended state; the events TASKS_RESUMED / TASKS_SUSPENDED record what it did.

**Steps.**

1. Get the full list. The callback stops at 20 objects:

   ```sql
   SELECT 'VIEW' AS KIND, v.TABLE_SCHEMA AS SCHEMA_NAME, v.TABLE_NAME AS OBJECT_NAME,
          REGEXP_SUBSTR(UPPER(v.VIEW_DEFINITION), 'ALFA_EDW_[A-Z0-9_]+') AS DB_NAMED
   FROM ALFA_EDW_SAN.INFORMATION_SCHEMA.VIEWS v
   WHERE REGEXP_INSTR(UPPER(v.VIEW_DEFINITION), 'ALFA_EDW_[A-Z0-9_]+') > 0
   UNION ALL
   SELECT 'FUNCTION', f.FUNCTION_SCHEMA, f.FUNCTION_NAME,
          REGEXP_SUBSTR(UPPER(f.FUNCTION_DEFINITION), 'ALFA_EDW_[A-Z0-9_]+')
   FROM ALFA_EDW_SAN.INFORMATION_SCHEMA.FUNCTIONS f
   WHERE REGEXP_INSTR(UPPER(f.FUNCTION_DEFINITION), 'ALFA_EDW_[A-Z0-9_]+') > 0
   UNION ALL
   SELECT 'PROCEDURE', p.PROCEDURE_SCHEMA, p.PROCEDURE_NAME,
          REGEXP_SUBSTR(UPPER(p.PROCEDURE_DEFINITION), 'ALFA_EDW_[A-Z0-9_]+')
   FROM ALFA_EDW_SAN.INFORMATION_SCHEMA.PROCEDURES p
   WHERE REGEXP_INSTR(UPPER(p.PROCEDURE_DEFINITION), 'ALFA_EDW_[A-Z0-9_]+') > 0
   ORDER BY 1, 2, 3;
   ```

   The query shows only the first database each definition names. Read the definition for the rest. Check tasks
   with `SHOW TASKS IN DATABASE ALFA_EDW_SAN;` and their `definition` column.
2. Find each object's source with `git grep -n -i "V_OPEN_ORDERS" -- migrations repeatable`.
   - **Not in git**: someone created or changed it outside Flyway. Treat it as drift (R5): the guard rightly blocks
     every deploy until it is resolved.
   - **In git**: FG009 should have rejected a literal. So the name came in some other way, for example dynamic SQL,
     a placeholder whose value names another runway, or `${flyway:database}`.
3. **Contain.** On a test runway, fix forward normally. On MGM or PRD (rehearsal should have caught it), treat a
   production object that reads or writes another runway as an incident, and fix it through R4.
4. **Fix.** Replace the 3-part name with `SCHEMA.OBJECT` when it is the same database, or with a declared placeholder
   for a non-EDW database (CONVENTIONS section 4). A repeatable re-runs on its own when edited. An object created by a
   V script needs a new V script that re-creates it.
5. Deploy the fix. The callback scans again and passes.

**Verify.** The query above returns only names of the runway's own database. The deploy is green with a SUCCEEDED row
in `FLYWAY.DEPLOY_LOG`.

**Do not.**
- Do not disable or edit the callback to get a deploy through. It protects every runway from cross-environment reads
  and writes.

---

## R11 Key rotation

**Who.**
- A Snowflake security admin (USERADMIN or SECURITYADMIN, whichever owns the service users) sets the keys.
- The Jenkins admin updates the credentials.
- The vault administrator replaces the vault entries that manual and break-glass sessions use (GUIDE B7).

**When.**
- Every quarter (GUIDE Part H).
- When someone with key access leaves.
- **Immediately** when a key may have been exposed.

**Steps.** Repeat them for each `SVC_EDW_FLYWAY_<ID>` user and for `SVC_EDW_FLYWAY_AUDIT`.

1. Generate a new encrypted key pair on an admin workstation, never on a shared agent:

   ```bash
   openssl genrsa 2048 | openssl pkcs8 -topk8 -v2 aes256 -inform PEM -out svc_edw_flyway_prd_2026q4.p8
   openssl rsa -in svc_edw_flyway_prd_2026q4.p8 -pubout -out svc_edw_flyway_prd_2026q4.pub
   ```
2. Find the free public-key slot. Look at `RSA_PUBLIC_KEY_FP` and `RSA_PUBLIC_KEY_2_FP`; one of them should be empty:

   ```sql
   DESC USER SVC_EDW_FLYWAY_PRD;
   ```
3. Load the new public key into the free slot. The value is the `.pub` body without its header and footer lines, on
   one line:

   ```sql
   USE ROLE USERADMIN;
   ALTER USER SVC_EDW_FLYWAY_PRD SET RSA_PUBLIC_KEY_2 = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA...';
   ```
4. Check the fingerprint. The output must equal `RSA_PUBLIC_KEY_2_FP` after its `SHA256:` prefix:

   ```bash
   openssl rsa -pubin -in svc_edw_flyway_prd_2026q4.pub -outform DER \
       | openssl dgst -sha256 -binary | openssl enc -base64
   ```
5. In Jenkins, update the credential `sf-flyway-PRD` in folder `EDW/deploy/PRD`: an SSH username with private key,
   username `SVC_EDW_FLYWAY_PRD`, the new `.p8` key and its passphrase, same id. For the auditor, update
   `sf-flyway-AUDIT` in `EDW/ci` and in every `EDW/deploy/<ID>` folder that holds it.
6. Replace the vault entry for `SVC_EDW_FLYWAY_PRD` (and for `SVC_EDW_FLYWAY_AUDIT`, if the vault holds it) with the
   new `.p8` and its passphrase, keeping the access list of GUIDE B7 (MGM and PRD: edw-breakglass only). Manual and
   break-glass sessions check the key out there (R0); a vault that still holds the old key fails the next incident
   session at login.
7. Prove the new key works, both copies of it. Run the PRD deploy job with `DRY_RUN=true`, which stops before Migrate
   (the Jenkins copy), and run `flyway -environment=PRD info` in an R0 session with the key checked out of the vault
   (the vault copy; for MGM and PRD this check-out is itself a ticketed break-glass step). Then check the logins:

   ```sql
   SELECT lh.EVENT_TIMESTAMP, lh.USER_NAME, lh.CLIENT_IP, lh.REPORTED_CLIENT_TYPE,
          lh.FIRST_AUTHENTICATION_FACTOR, lh.IS_SUCCESS
   FROM TABLE(ALFA_EDW_PRD.INFORMATION_SCHEMA.LOGIN_HISTORY_BY_USER(
            USER_NAME => 'SVC_EDW_FLYWAY_PRD', RESULT_LIMIT => 20)) lh
   ORDER BY lh.EVENT_TIMESTAMP DESC;
   ```
8. Remove the old key from the other slot. Next quarter, rotate into that slot:

   ```sql
   ALTER USER SVC_EDW_FLYWAY_PRD UNSET RSA_PUBLIC_KEY;
   ```
9. Destroy every copy of the old private key: the old vault version (or the vault's history of it, as your vault
   allows), any copy left from a break-glass session, and the generation files on the admin workstation
   (`shred -u`). Record the rotation date, the slot now in use and the fingerprint in the ticket.

**Suspected exposure.** Run step 8 for the exposed slot **first**, accepting that deploys stop, and then rotate.
Afterwards, check `LOGIN_HISTORY_BY_USER` for logins from addresses outside the Jenkins egress list.

**Verify.**
- `DESC USER` shows exactly one fingerprint, and it matches the new key.
- The dry run and the R0 `flyway info` with the vault copy both succeed with the new key.
- The vault entry's fingerprint matches `DESC USER`; no copy of the old key remains.

**Do not.**
- Do not paste a private key into a ticket, a chat or the repository. FG017 scans the kit's files for key material.

---

## R12 Rehearsal clone left behind

**Who.** The DBA, or the Jenkins admin.

**When.** `SHOW DATABASES LIKE 'ALFA_EDW_RH_%';` lists a database while no MGM or PRD deploy is running, typically
because a controller restart or a killed agent skipped the drop. The next rehearsal would replace it
(`CREATE OR REPLACE DATABASE ... CLONE`), but until then it costs storage, and it holds a copy of production data.

**Steps.**

1. Make sure no deploy is using it: the lock `ALFA_EDW_PRD` (or `ALFA_EDW_MGM`) is free on the Lockable Resources
   page. Reserve it (R0, step 1).
2. Look at the clone. The owner should be `EDW_PRD_DEPLOYER` or `EDW_MGM_DEPLOYER`:

   ```sql
   SHOW DATABASES LIKE 'ALFA_EDW_RH_%';
   ```
3. Drop it with the kit's validated SQL, which refuses any name other than `ALFA_EDW_RH_<ID>`. Use the deployer
   identity from R0:

   ```bash
   python3 scripts/fw.py rehearsal-sql --runway PRD --action drop > rh_drop.sql
   cat rh_drop.sql                  # DROP DATABASE IF EXISTS ALFA_EDW_RH_PRD;
   sfsql -f rh_drop.sql
   ```
4. Unreserve the lock.

**Verify.** `SHOW DATABASES LIKE 'ALFA_EDW_RH_%';` returns no rows.

**Do not.**
- Do not type a DROP DATABASE statement by hand. One wrong letter drops a runway.
- Do not drop a rehearsal clone while a deploy job holds the lock. That job is using it.
