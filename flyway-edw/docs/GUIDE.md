# ALFA EDW Flyway kit: setup and deployment guide

This guide takes the ALFA EDW from "six existing Snowflake databases" to "every change ships through Flyway
Enterprise and Jenkins", and then explains the everyday release path:

```
DEV  ->  test runway (SAN | SEA | PHX)  ->  MGM (preproduction)  ->  PRD (production)
```

It is written for four audiences: **DBAs** (Snowflake objects, adoption, break-glass), **data engineers** (writing and
shipping migrations), **release managers / approvers** (MGM and PRD), and **Jenkins / Bitbucket admins** (jobs,
credentials, branch protection).

How to read it:

* Steps are labelled by part (A1, B4, F3 ...). Each step says **Who**, **Where**, what to **Do** (exact commands or
  the kit file to run), how to **Verify**, and **What can go wrong**.
* `<ID>` is a runway id: `DEV`, `SAN`, `SEA`, `PHX`, `MGM` or `PRD`. The ids are also the Flyway environment names in
  `flyway.toml`, so `-environment=SAN` always means `ALFA_EDW_SAN`.
* Shell commands run from the kit root (the EDW repository root after step C1). SQL runs in a Snowsight worksheet
  unless the step says otherwise.
* Companion documents: [CONVENTIONS.md](CONVENTIONS.md) (how to write migrations),
  [SHORTCOMINGS.md](SHORTCOMINGS.md) (Flyway limits and the kit's mitigations), [RUNBOOKS.md](RUNBOOKS.md) (incident
  procedures R1-R12).
* Running Flyway by hand with `fw.py env` is step **B7**; enabling the Enterprise preview checks is **Part G**
  (spike G1, switch-on G2, drift gate G3).

---

## At a glance

### The runways

| Runway | Database     | Tier    | Lanes        | After       | Approval                    | Rehearsal clone | Tasks |
|--------|--------------|---------|--------------|-------------|-----------------------------|-----------------|-------|
| DEV    | ALFA_EDW_DEV | nonprod | main, branch | (first)     | none                        | no              | off   |
| SAN    | ALFA_EDW_SAN | nonprod | main, branch | DEV         | none (optional per runway)  | no              | off   |
| SEA    | ALFA_EDW_SEA | nonprod | main, branch | DEV         | none (optional per runway)  | no              | off   |
| PHX    | ALFA_EDW_PHX | nonprod | main, branch | DEV         | none (optional per runway)  | no              | off   |
| MGM    | ALFA_EDW_MGM | prod    | main         | SAN/SEA/PHX | edw-release-approvers       | ALFA_EDW_RH_MGM | on    |
| PRD    | ALFA_EDW_PRD | prod    | main         | MGM         | edw-prod-approvers + 4-eyes | ALFA_EDW_RH_PRD | on    |

"After" is the runway a bundle must already have completed on (`promote_from`; for MGM any one of the three test
runways). PRD also requires a change ticket. "Tasks" is the `tasks_enabled` placeholder in `flyway.toml`: where it is
off, every deploy leaves task graphs suspended. All policy columns come from `runways.toml`;
`python3 scripts/fw.py env --runway <ID>` prints them.

Names that follow the runway id (example: SAN):

| Item                         | Pattern                     | Example               | Defined in                 |
|------------------------------|-----------------------------|-----------------------|----------------------------|
| Deploy role (owns objects)   | `EDW_<ID>_DEPLOYER`         | EDW_SAN_DEPLOYER      | bootstrap/10               |
| Service user (key pair)      | `SVC_EDW_FLYWAY_<ID>`       | SVC_EDW_FLYWAY_SAN    | bootstrap/10               |
| Deploy warehouse             | `WH_EDW_DEPLOY_<ID>`        | WH_EDW_DEPLOY_SAN     | bootstrap/10               |
| ETL warehouse (`${etl_wh}`)  | `WH_EDW_ETL_<ID>`           | WH_EDW_ETL_SAN        | flyway.toml placeholders   |
| Jenkins deploy job           | `EDW/deploy/<ID>/deploy`    | EDW/deploy/SAN/deploy | jenkins/jobs.groovy        |
| Jenkins credential           | `sf-flyway-<ID>`            | sf-flyway-SAN         | runways.toml               |
| Jenkins lock                 | `ALFA_EDW_<ID>`             | ALFA_EDW_SAN          | runways.toml, casc-snippet |
| Bitbucket commit status      | `deploy-<ID>`               | deploy-SAN            | jenkins/Jenkinsfile.deploy |

Shared by all runways: the read-only auditor (role `EDW_FLYWAY_AUDITOR`, user `SVC_EDW_FLYWAY_AUDIT`, warehouse
`WH_EDW_FLYWAY_AUDIT`, Jenkins credential `sf-flyway-AUDIT`) and the network/authentication policies in
`EDW_ADMIN_DB.SECURITY`. Each runway database keeps its Flyway history in `<DB>.FLYWAY."flyway_schema_history"` (a
quoted lower-case name) plus the kit's own `FLYWAY.DEPLOY_LOG` and `FLYWAY.DEPLOY_TASK_STATE`.

### The promotion path

```mermaid
flowchart LR
    PR["Pull request<br/>guard + code review<br/>info/validate vs DEV"]
    MAIN["main build N<br/>immutable bundle"]
    PR -->|merge| MAIN
    MAIN -->|automatic| DEV[("DEV")]
    BR["branch bundle<br/>(project work)"] -.->|branch lane, manual| DEV
    BR -.->|branch lane, manual| TEST
    DEV -->|promote-check| TEST[("SAN / SEA / PHX<br/>leased test runways")]
    TEST -->|"any one passed<br/>rehearsal + approval"| MGM[("MGM<br/>preproduction")]
    MGM -->|"ticket + rehearsal<br/>+ four-eyes approval"| PRD[("PRD<br/>production")]
```

Every arrow deploys **the same bytes**: the bundle archived by one CI build, re-hashed by `fw.py verify-bundle`
before each deploy. MGM and PRD accept bundles built from `main` only.

### What the pipeline enforces and what is process

| Control                                               | Enforced by                                        | Auto   |
|-------------------------------------------------------|----------------------------------------------------|--------|
| Names, versions, applied-file immutability, risky SQL | `fw.py guard` (FG001-FG021), every PR build        | yes    |
| SQL code review (SQLFluff + rules/ALFA0x)             | `flyway check -code`, failOnError, PR builds       | yes    |
| Deploys started only by a user or the main CI build   | deploy job cause check (B7)                        | yes    |
| Deploy jobs never share agents with CI builds         | node restriction on the deploy agents (A2, C2)     | no     |
| No push to main; reviews; green build before merge    | Bitbucket branch restrictions (B5)                 | see B5 |
| One artifact for every runway                         | bundle + `fw.py verify-bundle` (sha256 manifest)   | yes    |
| MGM/PRD take main builds only                         | `verify-bundle --require-branch main`, lanes       | yes    |
| Order DEV -> test -> MGM -> PRD                       | `fw.py promote-check` on auditor `flyway info`     | yes    |
| One deploy per database at a time (no Flyway lock)    | Jenkins lock `ALFA_EDW_<ID>`                       | yes    |
| Rehearsal on a zero-copy clone before MGM/PRD         | deploy job, Rehearsal stage                        | yes    |
| Right approver group; PRD approver != requester       | input step submitter + four-eyes check             | yes    |
| Change ticket for PRD                                 | `CHANGE_TICKET` parameter required                 | partly |
| Out-of-band DDL since the last deploy                 | `fw.py drift-sql` / `drift-eval`, warn or fail     | yes    |
| Enterprise licence really active                      | licence stage (`flyway version` edition)           | yes    |
| No reference to another runway's database             | callback `afterMigrate__50_cross_env_guard`        | yes    |
| Task graphs restored (or kept off) after a deploy     | callbacks `beforeMigrate__20`, `afterMigrate__60`  | yes    |
| Deploy audit trail                                    | `FLYWAY.DEPLOY_LOG`, `deploy-<ID>` status, Jenkins | yes    |
| Service logins only from the Jenkins egress range     | network policy (B1)                                | yes    |
| Manual Flyway runs serialized with Jenkins            | reserve the Jenkins lock (B7)                      | no     |
| Runway leases, refreshes, data-owner sign-off         | F2, bootstrap/30                                   | no     |
| No person holds a deploy role; keys in the vault      | B7 + quarterly audit (H3)                          | no     |
| Hotfix cherry-pick, undo, Time Travel restore         | F5, F6, RUNBOOKS                                   | no     |
| Key rotation, licence renewal, access reviews         | H3                                                 | no     |

"partly": the job refuses to run PRD without a ticket number; whether the ticket is approved is the approver's check.

### What one deploy job run does

Every runway has one job, `EDW/deploy/<ID>/deploy`, built from `jenkins/Jenkinsfile.deploy`:

1. **Resolve policy**: `fw.py env --runway <ID> --format json` (nothing about a runway is hard-coded in Groovy), and
   check who started the run: a user (Build with Parameters), or the CI main job's own trigger of DEV; anything
   else is refused (B7).
2. **Fetch bundle**: copy the archived bundle of `SOURCE_JOB` build `SOURCE_BUILD`, check the lane, run
   `fw.py verify-bundle` (main-only for MGM/PRD) and run main's `fw.py guard` over the bundle's files.
3. **Licence check**: stop unless `flyway version -outputType=json` reports the Enterprise edition.
4. **Promotion check**: `flyway info` and `flyway validate` on each predecessor with the read-only auditor login,
   then `fw.py promote-check` (passes if ANY one predecessor ran the whole bundle cleanly).
5. Inside the lock `ALFA_EDW_<ID>` (plus `ALFA_EDW_RH_<ID>` for MGM/PRD): pre-flight `info`/`validate` on the
   target and the baseline check; drift check (with the target's deploy login); Enterprise `check -drift`
   (advisory, only when `preview_checks = true`); `check -dryrun` report (MGM/PRD, and any `DRY_RUN`);
   **rehearsal** (MGM/PRD: clone, migrate the clone, drop it whatever happens); **approval**; record the **Time
   Travel point**; **migrate**; archive `flyway info` JSON; post the `deploy-<ID>` status on the bundle's commit in
   Bitbucket (main lane only: a status on a feature branch's head would count in that branch's pull request).

Parameters: `TARGET` (must match the folder's runway), `SOURCE_JOB` (default `/EDW/ci/edw-flyway/main`),
`SOURCE_BUILD`, `CHANGE_TICKET` (required for PRD), `DRY_RUN` (every check, the dry-run report and the rehearsal,
then stop: the target is not changed and no approval is asked), `ACK_DRIFT` (an approver accepts reported drift
when the runway's `drift_gate` is `fail`). The job always refuses a database whose `flyway info` shows no schema
version, i.e. one that was never baselined; there is no override (a new database is baselined first, D5).

---

## Part A - Decisions and prerequisites

### A1. Confirm the design decisions

**Who:** EDW platform owner, lead DBA, security officer, release manager. **Where:** a design review; record the
outcome in your architecture/change log.

**Do:** confirm each decision below, or stop and adapt the kit first.

1. **One Snowflake account** holds all six databases. Locks, rehearsal clones and runway refreshes are zero-copy
   clones inside one account; a second account would need replication and is out of scope.
2. **Flyway Enterprise, one pinned version** (`runways.toml` `[kit] flyway_version` and `flyway_image`, today
   13.8.1; guard FG019 makes them agree). Snapshot history on Snowflake needs 13.8.0 or later.
3. **Migrations are the only way DDL reaches MGM and PRD.** Anything else is drift and is reported at the next deploy.
4. **Ownership model:** each `ALFA_EDW_<ID>` database stays owned by SYSADMIN; every schema and object inside it is
   owned by `EDW_<ID>_DEPLOYER` (step D2). Object ownership is what lets one role run every migration.
5. **Code never names its own database** (FG009). Views, procedures and tasks use `SCHEMA.OBJECT`, which Snowflake
   resolves inside the object's own database, so one script works in all six databases and in clones.
6. **Production data in clones.** MGM/PRD deploys rehearse on a zero-copy clone of the target (`ALFA_EDW_RH_<ID>`,
   owned by the deploy role, dropped by the job), and test runways are refreshed from PRD by clone
   (`bootstrap/30_refresh_runway.sql`). Both copy production data. The data owner and security must sign off:
   who may read a refreshed runway, whether masking and row access policies come along (policies kept in another
   database keep pointing at PRD's), PII handling, and retention. Rehearsal is mandatory for prod-tier runways
   (FG019); runway refresh by clone is optional; without sign-off, seed test runways another way.
7. **`main` is the only release branch.** MGM and PRD deploy main bundles only.
8. **Fix-forward is the default rollback.** Snowflake DDL autocommits, and on Snowflake Flyway runs every migration in
   autocommit (DML too, SHORTCOMINGS S02), so a failed migration is partially applied (RUNBOOKS R1). Undo scripts and
   Time Travel are the exceptions (F6).

**Verify:** the decision record is signed by the platform owner and security.

**What can go wrong:** skipping decision 6 puts PII in test runways; a team that still edits PRD by hand turns every
deploy into a drift alarm.

### A2. Licences, plans and tooling

**Who:** platform owner, Jenkins admin, network team. **Where:** Redgate and Atlassian admin portals, Jenkins.

**Do:**

1. **Flyway Enterprise licence.** A Redgate account with the Enterprise entitlement for CI (a team identity, not a
   person) plus one per engineer who runs Enterprise commands by hand (B6).
2. **Bitbucket Cloud plan.** Enforced merge checks ("prevent a merge with unresolved merge checks") and workspace or
   project access tokens are believed to need **Premium**: verify on your workspace's plan page. On Standard the
   merge checks are advisory and a repository access token is the Jenkins credential.
3. **Jenkins controller plugins:** Pipeline (workflow-aggregator, pipeline-model-definition), bitbucket-branch-source,
   cloudbees-folder, credentials-binding, docker-workflow, lockable-resources, copyartifact, pipeline-utility-steps,
   pipeline-input-step, pipeline-build-step, junit, timestamper, role-strategy, job-dsl, configuration-as-code;
   job-restrictions, unless the deploy agents come from a separate cloud the CI folder cannot use (C2 step 7);
   htmlpublisher only if you want the Flyway HTML reports rendered inside Jenkins (C2).
4. **Agents:** Linux agents that can run Docker containers, in **two separate pools**:
   * **CI agents** (label `EDW_AGENT_LABEL`) run the `EDW/ci` builds, including every pull request's own Jenkinsfile.
   * **Deploy agents** (label `EDW_DEPLOY_AGENT_LABEL`) run only the `EDW/deploy/*` jobs. They hold the runway keys
     while a deploy runs: credentials bound around a docker `.inside` step are visible in the deploy host's process
     list, and a build can leave files or cached images behind for the next one. So these hosts are dedicated to
     the deploy jobs, with shell access for the Jenkins admins only, and the node side enforces the split (C2
     step 7): a label alone is not enough, because a PR's Jenkinsfile can ask for any label. Ideally MGM and PRD use
     single-use (ephemeral) agents.

   Flyway runs inside `redgate/flyway:<flyway_version>` (Ubuntu 24.04 base: it carries the GLIBC 2.35+ that the
   bundled SQLFluff needs and the native libraries of the comparison engine); `fw.py` and the Snowflake CLI run in
   `runways.toml` `python_image`. The CI Guard stage also needs **git** in that image (FG003/FG004 compare with the
   base branch), and `python:3.11-slim` has none. Recommended: build an internal image from `python:3.11-slim` plus
   git and `snowflake-cli==3.28.0` (`[kit] snowflake_cli`), and point `python_image` at it. Otherwise the CI
   Jenkinsfile installs git with apt-get on every build, which needs the apt route in point 5. Pin both images by
   digest in `runways.toml`, keeping the version tag in front of it (`redgate/flyway:13.8.1@sha256:<digest>`;
   FG019 ties the Flyway tag to `flyway_version`), so neither a re-pushed tag nor an agent's image cache decides what
   runs in a deploy. If you ever run Flyway natively on an agent or workstation, it needs GLIBC 2.35+ (RHEL 9 /
   Ubuntu 22.04 or later) for `check -code`.
5. **Network egress from the agents:** the Snowflake account host and the hosts returned by
   `SELECT SYSTEM$ALLOWLIST();`; Redgate licensing (online PAT licensing; Redgate does not document the host list in
   the CLI docs, so ask Redgate or use an offline permit); your Docker registry (or a mirror holding the two images);
   PyPI or a mirror (`pip install snowflake-cli==3.28.0`); `bitbucket.org` and `api.bitbucket.org`; and, only while
   the python image has no git, the Debian mirrors `deb.debian.org` and `security.debian.org` (or an internal apt
   mirror). **Ingress:** Bitbucket Cloud must reach `https://<jenkins>/bitbucket-scmsource-hook/notify` for webhooks;
   otherwise configure periodic branch indexing.
6. **Proxy:** Flyway 13.9+ ignores operating-system proxy settings, and on Linux/Docker the JVM never reads
   `HTTPS_PROXY`. Behind a proxy, set the controller environment variable `EDW_JAVA_PROXY_ARGS` to
   `-Dhttps.proxyHost=<host> -Dhttps.proxyPort=<port>` (the Jenkinsfiles append it to `JAVA_ARGS`; see
   `casc-snippet.yaml`), and put the same properties in `JAVA_ARGS` for manual runs. Set it now so a later upgrade
   past 13.9 does not break licensing. A PyPI mirror goes into the controller variable `PIP_INDEX_URL`. apt (the
   git fallback of point 4) reads only the lower-case `http_proxy` / `https_proxy`; the CI Guard stage derives them
   from the controller's `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` (`http_proxy` falls back to `HTTPS_PROXY`). Set
   the upper-case names only: Jenkins treats variable names case-insensitively, so a lower-case duplicate would
   replace the upper-case entry. An internal apt mirror works too.

**Verify** on one agent (the image's entrypoint is `flyway`):

```bash
docker run --rm -e FLYWAY_EMAIL -e FLYWAY_TOKEN -e JAVA_ARGS redgate/flyway:13.8.1 version -outputType=json
```

The JSON must show the Enterprise edition and a `permitExpiry` in the future.

**What can go wrong:** exit code **35** is a licensing failure (wrong PAT, no route to Redgate, proxy not in
`JAVA_ARGS`). An expired permit falls back to the Community feature set; the pipeline's licence stage turns that into
a failure, a manual run will not.

---

## Part B - Accounts and access

### B1. Shared Snowflake security objects and the auditor

**Who:** Snowflake admin (SECURITYADMIN / SYSADMIN; ACCOUNTADMIN only where the script says so). **Where:** Snowsight,
`bootstrap/10_account_objects.sql`, shared sections.

**Do:**

1. Edit the `SET` variables at the top: the Jenkins agents' egress CIDRs and the admin location (default
   `EDW_ADMIN_DB.SECURITY`).
2. Run the shared sections: admin database/schema, network rule and network policy, authentication policy
   (`AUTHENTICATION_METHODS = ('KEYPAIR')`, `CLIENT_TYPES = ('DRIVERS', 'SNOWFLAKE_CLI')`: JDBC for Flyway, the
   Snowflake CLI for the drift and rehearsal SQL), and the auditor role
   `EDW_FLYWAY_AUDITOR`, warehouse `WH_EDW_FLYWAY_AUDIT` and user `SVC_EDW_FLYWAY_AUDIT` (its key pair is made exactly
   like B2's).

**Verify:**

```sql
SHOW NETWORK RULES IN SCHEMA EDW_ADMIN_DB.SECURITY;
SHOW NETWORK POLICIES;
SHOW AUTHENTICATION POLICIES IN SCHEMA EDW_ADMIN_DB.SECURITY;
DESC USER SVC_EDW_FLYWAY_AUDIT;          -- TYPE = SERVICE, RSA_PUBLIC_KEY_FP set
SHOW GRANTS TO ROLE EDW_FLYWAY_AUDITOR;
```

**What can go wrong:**

* Wrong CIDRs: every Jenkins login fails with a network-policy error. Fix the network rule, not the policy.
* The pipeline also runs the **Snowflake CLI** (`snow sql`) for drift and rehearsal SQL, which is why the policy
  allows `SNOWFLAKE_CLI` next to `DRIVERS`. If those logins are still refused while Flyway's JDBC logins work, check
  the client type Snowflake recorded (`REPORTED_CLIENT_TYPE` in `LOGIN_HISTORY_BY_USER`, RUNBOOKS R11 step 7);
  Snowflake documents client-type checks as best effort.
* Policies created inside an `ALFA_EDW_*` database would be cloned and replaced with it: keep them in the admin
  database.

### B2. Deploy identity per runway (run six times)

**Who:** Snowflake admin; a second person if your key-custody policy requires two. **Where:** a secured admin
workstation (OpenSSL) and Snowsight (`bootstrap/10_account_objects.sql`, per-runway section).

**Do:**

1. Generate an **encrypted PKCS#8** key pair; one per runway, never shared between runways:

   ```bash
   umask 077
   U=SVC_EDW_FLYWAY_DEV
   openssl genrsa 2048 | openssl pkcs8 -topk8 -v2 aes256 -inform PEM -out "$U.p8"   # asks for a passphrase
   openssl rsa -in "$U.p8" -pubout -out "$U.pub"                                      # asks for it again
   grep -v -- '-----' "$U.pub" | tr -d '\n'; echo                                     # one-line public key
   ```

2. In bootstrap/10 PART 0 set `runway = 'DEV'` (then SAN, SEA, PHX, MGM, PRD) and `access_roles` (B3 step e), paste
   the one-line public key (no BEGIN/END lines) and run PART 0 + 2 + 3 + 4b + 5. It creates `WH_EDW_DEPLOY_<ID>`,
   `EDW_<ID>_DEPLOYER` (granted to SYSADMIN), `SVC_EDW_FLYWAY_<ID>` (`TYPE = SERVICE`, `DEFAULT_ROLE`,
   `DEFAULT_SECONDARY_ROLES = ()`, network and authentication policy) and the runway's access roles (PART 2e). For
   MGM and PRD it also grants CREATE DATABASE to the deploy role so the job can create the rehearsal clone; the
   script explains that trade-off.
3. Account-level task and alert privileges come from bootstrap/10 PART 2c (ACCOUNTADMIN); they are account grants
   and never belong in a migration (FG007):
   * `EXECUTE TASK` on every runway: a task runs, and resumes, only if its owner role has it.
   * `EXECUTE ALERT` on **MGM and PRD, automatically**. They are the runways whose deploys resume alerts
     (`tasks_enabled = "true"`), and without it the first MGM deploy of a layer-85 file would fail at the resume,
     after its approval (CONVENTIONS 5.12). DEV, SAN, SEA and PHX need it only if they resume alerts (a project that
     switches tasks on, or adopting a database with running alerts, D2): then run the
     `GRANT EXECUTE ALERT ON ACCOUNT TO ROLE IDENTIFIER($deploy_role);` line from PART 2c's comment for that runway.
   * Serverless tasks or alerts (no WAREHOUSE) also need `EXECUTE MANAGED TASK` / `EXECUTE MANAGED ALERT`, also in
     that comment.
4. Store the `.p8` file and its passphrase in the enterprise vault (manual and break-glass sessions check them out
   there, B7), give them to the Jenkins admin for B4 through your secret-transfer channel, then delete the local
   copies (`shred -u "$U.p8"`).

**Verify:** compare fingerprints, then the grants:

```bash
openssl rsa -pubin -in "$U.pub" -outform DER | openssl dgst -sha256 -binary | openssl enc -base64
```

```sql
DESC USER SVC_EDW_FLYWAY_DEV;    -- RSA_PUBLIC_KEY_FP (or _2_FP) = 'SHA256:' + the value above; TYPE = SERVICE
SHOW GRANTS OF ROLE EDW_DEV_DEPLOYER;    -- exactly SYSADMIN (role) and SVC_EDW_FLYWAY_DEV (user)
SHOW GRANTS TO ROLE EDW_DEV_DEPLOYER;    -- warehouse USAGE, EXECUTE TASK; MGM/PRD: CREATE DATABASE, EXECUTE ALERT
```

bootstrap/10 PART 5 runs these checks for the runway in PART 0 and prints one PASS / FAIL / CHECK / INFO line each.
It accepts the key in either slot (`RSA_PUBLIC_KEY_FP` or `RSA_PUBLIC_KEY_2_FP`: after a rotation the live key may be
in slot 2) and fails a key shared by two service users; it wants `EXECUTE ALERT` on MGM and PRD (INFO elsewhere);
it lists the databases the deploy role owns (none, or on MGM/PRD only `ALFA_EDW_RH_<ID>` while a deploy job
rehearses: otherwise RUNBOOKS R12); and it checks that every access role named in `access_roles` exists.

**What can go wrong:** pasting the BEGIN/END lines, or a PKCS#1 key (`BEGIN RSA PRIVATE KEY`), fails the login; the
JDBC driver needs `-Dnet.snowflake.jdbc.useBundledBouncyCastleForPrivateKeyDecryption=true` in `JAVA_ARGS` to read an
encrypted key (the Jenkinsfiles set it; B7 shows it for manual runs); a user whose default secondary roles are not
`()` deploys with more privileges than the deploy role, and objects may then reference things other runways cannot
see.

### B3. Runway database grants and the FLYWAY schema (run six times)

**Who:** Snowflake admin. **Where:** Snowsight, `bootstrap/20_runway_database.sql` with `runway = '<ID>'`.

**Do:** run PART 0, 1, 2 and 6. They (a) check the database exists and is owned by SYSADMIN, (b) grant USAGE and
CREATE SCHEMA on it to `EDW_<ID>_DEPLOYER`, (c) create the `FLYWAY` schema (managed access) owned by the deploy role
with `FLYWAY.DEPLOY_LOG` and `FLYWAY.DEPLOY_TASK_STATE`, (d) give the auditor USAGE on the database and the `FLYWAY`
schema, and (e) give the runway's access roles USAGE on the database; PART 6 prints a PASS / FAIL report. **Do
not** run the brownfield ownership transfer yet (PART 3, that is D2) or the post-baseline auditor grant (PART 5, D5).

(e) The **access roles** are `EDW_<ID>_<SCHEMA>_READ` (and `EDW_<ID>_<SCHEMA>_WRITE` where a schema uses one), for
every schema that has, or will get, a layer-90 grants file. One list in bootstrap/10 PART 0 names them for all
runways:

```sql
SET access_roles = 'SALES_READ,FINANCE_READ';    -- <SCHEMA>_READ / <SCHEMA>_WRITE entries, comma-separated
```

bootstrap/10 PART 2e (part of B2's per-runway run, or PART 0 + 2e on its own) has USERADMIN create
`EDW_<ID>_<entry>` for each entry and SECURITYADMIN grant each role to SYSADMIN. bootstrap/20 PART 1 then grants
USAGE on the runway database to every existing `EDW_<ID>_*_READ` / `_WRITE` role; that is a database-level grant,
so it never sits in a grants file (FG007). The grants files grant to these roles through `${role_prefix}`, a
migration never creates a role (FG007), and a missing role fails that runway's grants file at deploy time
(CONVENTIONS 5.13). When a new schema gets a grants file, add its entries to `access_roles` and run bootstrap/10
PART 0 + 2e, then bootstrap/20 PART 0 + 1, for every runway, before that PR merges.

**Kit upgrade on an existing runway.** Before the first deploy of a new kit version (callbacks, bootstrap) to a
runway that already has its `FLYWAY` schema, re-run bootstrap/20 PART 0 + 2 + 6 there, on every runway. PART 2 is
idempotent and adds the kit-table columns the new callbacks write; this version adds
`FLYWAY.DEPLOY_TASK_STATE.ROOT_FQN`. Without it every deploy to that runway stops in `beforeMigrate__20_task_snapshot`
with error -20121, before anything is applied. PART 6's "kit table columns" line shows whether the tables are current.

The grant model rests on **managed-access schemas**: every EDW schema is created `WITH MANAGED ACCESS` (new ones by
their V migration, adopted ones by bootstrap/20 in D2), so the deploy role, as schema owner, can define the
schema-level future grants of the layer-90 files without the account-wide MANAGE GRANTS privilege, which the kit
never grants.

**Verify:**

```sql
SHOW GRANTS ON DATABASE ALFA_EDW_DEV;            -- OWNERSHIP: SYSADMIN; USAGE + CREATE SCHEMA: EDW_DEV_DEPLOYER;
                                                 -- USAGE: EDW_FLYWAY_AUDITOR and the access roles of (e)
SHOW SCHEMAS LIKE 'FLYWAY' IN DATABASE ALFA_EDW_DEV;   -- owner EDW_DEV_DEPLOYER, options MANAGED ACCESS
SHOW TABLES IN SCHEMA ALFA_EDW_DEV.FLYWAY;       -- DEPLOY_LOG, DEPLOY_TASK_STATE
SHOW ROLES LIKE 'EDW_DEV_%';                     -- EDW_DEV_DEPLOYER plus the access roles of (e)
```

and bootstrap/20 PART 6 shows PASS or INFO lines, except its schema and task lines: they stay CHECK / FAIL until
PART 3 (D2) has handed every existing schema, PUBLIC included, to the deploy role and made it managed access.

**What can go wrong:** `flyway.toml` sets `createSchemas = false`, so Flyway never creates `FLYWAY` itself; if it is
missing, `baseline` and `migrate` fail. The two kit tables make `FLYWAY` non-empty, which is deliberate: Flyway
checks only its configured schemas (`FLYWAY`), so a `migrate` on a database that was never baselined stops with
"Found non-empty schema(s) ... but no schema history table" instead of replaying every migration against live data.
A grants file that names a role (e) did not create fails on that runway, and its Failed row blocks every later
deploy there until RUNBOOKS R1.

### B4. Jenkins credentials

**Who:** Jenkins admin. **Where:** Jenkins, each folder's Credentials page. The folders are created by the seed job
(C3); if they do not exist yet, do C2-C3 first and come back.

**Do:** create exactly these IDs (case-sensitive) in exactly these folders. Never at the Jenkins root (System or
Global): anything visible to `/EDW/ci` can be used by any pull request, because a PR runs its own Jenkinsfile.

| ID                       | Kind                          | Folders                  | Holds                        |
|--------------------------|-------------------------------|--------------------------|------------------------------|
| `sf-account`             | Secret text                   | `/EDW`                   | account identifier (below)   |
| `sf-flyway-AUDIT`        | SSH Username with private key | `/EDW/ci` + every deploy | SVC_EDW_FLYWAY_AUDIT key     |
| `flyway-license`         | Username with password        | `/EDW/ci` + every deploy | Redgate email + PAT (B6)     |
| `bitbucket-status-token` | Secret text                   | `/EDW/ci` + every deploy | repository access token (B5) |
| `bitbucket-checkout`     | Username with password        | `/EDW/deploy`            | x-token-auth + same token    |
| `sf-flyway-<ID>`         | SSH Username with private key | `/EDW/deploy/<ID>` only  | SVC_EDW_FLYWAY_<ID> key      |

"Every deploy" means a separate copy in each of the six `/EDW/deploy/<ID>` folders. `bitbucket-status-token` is
also the multibranch project's scan credential. `bitbucket-checkout` (username `x-token-auth`, password = the same
repository access token) lets the deploy jobs check out `jenkins/Jenkinsfile.deploy` from `main`: an SCM checkout
cannot use a Secret text credential.

* "SSH Username with private key" is used only as a container: username = the Snowflake service user, private key =
  the contents of the `.p8` file (`-----BEGIN ENCRYPTED PRIVATE KEY-----` ...), passphrase = its passphrase. The
  pipeline binds them to `FLYWAY_SF_USER`, `FLYWAY_SF_KEY_FILE` and `FLYWAY_SF_KEY_PWD`, which `flyway.toml` reads.
* `sf-account` is the account identifier as it appears in your Snowflake URL, preferably `<orgname>-<account_name>`
  (`SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME();`); a legacy locator needs its region suffix.

**Verify:** each folder's Credentials page lists the IDs above and nothing for other runways; `/EDW/deploy/PRD` is the
only place `sf-flyway-PRD` exists. The first PR build (C4) and each first deploy (H1) prove the bindings.

**What can go wrong:** a wrong credential kind makes the binding step fail ("no suitable binding handler"); a deploy
key placed in `/EDW` or at root is readable by every PR build; rotating `sf-flyway-AUDIT` means updating every copy.

### B5. Bitbucket: Jenkins token, branch restrictions, CODEOWNERS

**Who:** Bitbucket repository admin (plus the workspace admin for groups). **Where:** bitbucket.org repository
settings; a workstation with `bash`, `curl` and `jq`.

**Do:**

1. **Repository access token for Jenkins** (Repository settings > Access tokens): scopes Repositories: Read and Pull
   requests: Read; add Webhooks: Read and write only if Jenkins should register its own webhook. Store it as
   `bitbucket-status-token` (B4). Posting a commit status needs only repository read. App passwords are gone; do not
   use a person's API token (it leaves with the person and expires).
2. **Branch restrictions on `main`**, applied as code from `bitbucket/branch-restrictions.json` (idempotent: it
   creates or updates, never deletes). Create the Bitbucket groups first (Workspace settings > User groups), then:

   ```bash
   export BITBUCKET_MERGE_GROUPS=edw-developers          # Bitbucket group slugs allowed to merge into main
   export BITBUCKET_BREAK_GLASS_GROUPS=edw-breakglass    # allowed to push directly; unset = nobody
   bitbucket/apply_branch_restrictions.sh --workspace <workspace> --repo <edw-repo> --dry-run   # review payloads
   read -rsp 'admin token: ' BITBUCKET_ADMIN_TOKEN && export BITBUCKET_ADMIN_TOKEN && echo
   bitbucket/apply_branch_restrictions.sh --workspace <workspace> --repo <edw-repo>
   unset BITBUCKET_ADMIN_TOKEN
   ```

   The admin token needs repository admin scope (a repository access token with Repositories: Admin works); make it
   short-lived and never store it in Jenkins. On a plan that refuses `enforce_merge_checks`, re-run with
   `--skip enforce_merge_checks`. Add the DBAs / data architects as **default reviewers** (individual users), or the
   "one default-reviewer approval" rule can never be met.
3. **CODEOWNERS:** after C1 the file lives at `.bitbucket/CODEOWNERS`. Replace `alfa` with the workspace slug and the
   example groups with yours; platform paths (`flyway.toml`, `runways.toml`, `callbacks/`, `jenkins/`, `bootstrap/`,
   `rules/`, `scripts/`) belong to the platform team.

**Verify:** Repository settings > Branch restrictions shows `main` with: no direct push except the break-glass group,
merge via pull request, 2 approvals, 1 default-reviewer approval, at least 1 passing build, no changes requested, all
tasks resolved, approvals reset on change. A test `git push origin main` from a normal account is rejected; a test
PR shows the Jenkins build status (after C4).

**What can go wrong:** without Premium the checks may be advisory (verify), in which case the real gate is that MGM and
PRD only take main bundles that passed the pipeline; CODEOWNERS adds owners as reviewers but does not block a merge
on its own; renaming or moving the Jenkins multibranch job changes the status key, and an old FAILED status keeps
blocking that commit until a new commit is pushed; apply the script to a scratch repository first and confirm with a
dummy PR that an empty push list still allows "merge via pull request" for the merge group.

### B6. Redgate licence for CI and for people

**Who:** licence admin, Jenkins admin. **Where:** Redgate portal, Jenkins.

**Do:**

1. **CI:** sign in as the CI identity, create a personal access token at
   `https://identityprovider.red-gate.com/personaltokens`, and store email + PAT as `flyway-license` (B4). The
   Jenkinsfiles export them as `FLYWAY_EMAIL` / `FLYWAY_TOKEN`; PATs refresh automatically.
2. **No internet from Jenkins?** Use an offline permit (`https://permits.red-gate.com/offline?productCode=63`),
   store it as a Secret file, and export `FLYWAY_OFFLINE_PERMIT_PATH` instead. That requires changing the licence
   binding in both Jenkinsfiles; record the permit's expiry in H3.
3. **People:** each engineer who runs Enterprise or Teams features by hand (`undo`, `cherryPick`, `check`, scripts
   with `shouldExecute`) creates their own PAT and exports `FLYWAY_EMAIL` / `FLYWAY_TOKEN` in their shell (B7).
4. Never use `flyway auth` in CI (interactive browser flow) or the deprecated `licenseKey`.

**Verify:** `flyway version -outputType=json` (A2 command) shows the Enterprise edition; the licence stage of the
first PR build passes.

**What can go wrong:** exit code 35 (licensing; check `JAVA_ARGS` proxy settings); an expired permit silently
reduces a manual run to the Community feature set. Flyway warns 30 days before expiry; H3 tracks it.

### B7. People: groups, human access, break-glass, running Flyway by hand

**Who:** security/IAM, lead DBA, Jenkins admin. **Where:** Active Directory, the enterprise vault, Jenkins Role
Strategy (`jenkins/casc-snippet.yaml`), Bitbucket groups, a host inside the Jenkins egress range.

**Do:**

1. **Create the AD groups** (suggested names; whatever you choose must match `runways.toml` `approval_group` and the
   role assignments in `casc-snippet.yaml`, because Jenkins matches the security realm's group names):

   | Group                          | Members                     | Grants                                            |
   |--------------------------------|-----------------------------|---------------------------------------------------|
   | edw-developers                 | data engineers              | Bitbucket write (branches, PRs); Jenkins CI build |
   | edw-dba                        | DBAs                        | CODEOWNERS; Build DEV/SAN/SEA/PHX; reserve locks  |
   | edw-runway-leads-SAN/-SEA/-PHX | lead of the leasing project | Build DEV + `EDW/deploy/<ID>`; opt. approval      |
   | edw-release-approvers          | release managers            | Build + approve MGM; start PRD                    |
   | edw-prod-approvers             | two or more named approvers | Build + approve PRD (four-eyes)                   |
   | edw-jenkins-admins             | Jenkins admins              | Job/Configure on the deploy folders (only them)   |
   | edw-breakglass                 | two or three senior DBAs    | push to `main`; MGM/PRD key checkout (step 3)     |

   `casc-snippet.yaml` assigns exactly these group names. Nobody outside edw-jenkins-admins gets Job/Configure or
   Run/Replay on `EDW/deploy/*`: either would let them edit the approval out of a deploy.

   These Build permissions hold only for runs a person starts. A pipeline `build` step runs as SYSTEM, so a branch or
   PR Jenkinsfile could otherwise start `EDW/deploy/SAN/deploy` with its own branch bundle. The deploy job therefore
   accepts a run only when a **user started it** (Jenkins has checked that user's Build permission on the job), or
   when it is the **CI main job's own DEV trigger** (one upstream cause from `EDW/ci/edw-flyway/main`, the same
   build as `SOURCE_BUILD`, `TARGET = DEV`). Every other cause, such as an upstream branch or PR job, a remote
   trigger token or a timer, is refused with an error. (A REST call made with a person's own API token counts as that
   person starting the run.)

2. **Human Snowflake access.** People never hold an `EDW_<ID>_DEPLOYER` role: on every runway
   `SHOW GRANTS OF ROLE` lists only SYSADMIN and the runway's service user. People work with functional roles,
   which are granted the runway's access roles (B3 step e), and `EDW_FLYWAY_AUDITOR` is granted on request to read
   Flyway history. When someone must run Flyway or
   deploy-role SQL by hand (RUNBOOKS R0), they use the runway's **service login**, whose encrypted key and
   passphrase live in the enterprise vault (B2 step 4). Vault access per runway:

   | Runway      | May check out the `SVC_EDW_FLYWAY_<ID>` key   | Condition                          |
   |-------------|-----------------------------------------------|------------------------------------|
   | DEV         | edw-dba                                       | none                               |
   | SAN/SEA/PHX | edw-dba and the current lease's lead          | during the lease                   |
   | MGM, PRD    | edw-breakglass                                | break-glass session only (step 3)  |

   SYSADMIN holders inherit every deploy role through the role hierarchy: keep that group small and treat any DDL
   they run in MGM/PRD as break-glass.

3. **Break-glass for MGM/PRD** (incident, emergency hotfix F5, adoption D5; PRD shown, MGM likewise):
   1. An emergency change ticket, approved by the release manager and by a member of edw-prod-approvers other than
      the operator.
   2. In Jenkins, a DBA **reserves** the lockable resource `ALFA_EDW_PRD` (Lockable Resources page, Reserve) so no
      deploy job can start; Flyway itself takes no lock on Snowflake.
   3. The operator (edw-breakglass) checks the `SVC_EDW_FLYWAY_PRD` key and passphrase out of the vault onto the
      session host only.
   4. The operator works as in step 4 (RUNBOOKS R0) and records every command and its output in the ticket.
   5. Unreserve the lock, delete the key copy and the working folder, and codify any hand-made DDL as a migration the
      same day (RUNBOOKS R5). If the key copy ever left the session host, rotate the key (RUNBOOKS R11).

4. **Running Flyway by hand** (RUNBOOKS R0 is the same procedure in runbook form).
   * **Where:** a host inside the range the service users' network policy allows (the Jenkins agents' egress CIDRs
     from B1); in practice a Jenkins agent host. `SVC_EDW_FLYWAY_*` logins are refused from anywhere else, so a
     laptop session fails at login.
   * **What:** the bundle that is deployed (or about to be), verified with `fw.py` from a clean checkout of `main`,
     never a feature-branch checkout. Tools: Docker or the Flyway CLI at exactly `runways.toml` `flyway_version`
     (never the open-source `flyway/flyway` image), `snowflake-cli` at `[kit] snowflake_cli`, Python 3.11+.

   ```bash
   # from a clean checkout of main; for MGM/PRD add --require-branch main
   python3 scripts/fw.py verify-bundle --bundle edw-flyway-bundle-<N>.tar.gz --extract work
   cd work/bundle
   eval "$(python3 scripts/fw.py env --runway DEV --format shell)"   # service user, role, warehouse, EDW_* policy
   export SNOWFLAKE_ACCOUNT='<orgname>-<account_name>'
   export FLYWAY_SF_KEY_FILE="$HOME/secure/svc_edw_flyway_dev.p8"     # checked out of the vault
   read -rsp 'key passphrase: ' FLYWAY_SF_KEY_PWD && export FLYWAY_SF_KEY_PWD && echo
   read -rsp 'Redgate PAT: ' FLYWAY_TOKEN && export FLYWAY_TOKEN && echo
   export FLYWAY_EMAIL='<licensed Redgate email>'
   export JAVA_ARGS="-Duser.home=$PWD/.flyway-home"                    # licence cache stays in the work folder
   JAVA_ARGS="$JAVA_ARGS -Dnet.snowflake.jdbc.useBundledBouncyCastleForPrivateKeyDecryption=true"
   flyway version -outputType=json                                     # "edition" must be Enterprise
   flyway -environment=DEV info
   ```

   `fw.py env` prints what `flyway.toml` resolves (`FLYWAY_SF_USER` = the service user, `FLYWAY_SF_ROLE`,
   `FLYWAY_SF_WAREHOUSE`, and `FLYWAY_RH_DB` for MGM/PRD) plus the runway policy under `EDW_*` names. Policy never
   uses `FLYWAY_*` names because Flyway reads variables such as `FLYWAY_TARGET`, `FLYWAY_ENVIRONMENT` and
   `FLYWAY_BASELINE_VERSION` as its own settings: start from a clean shell and export no other `FLYWAY_*` name. The
   account, key file, passphrase and licence are always exported by hand. (`--auditor` prints the read-only login
   the PR builds and promotion checks use; people read history with SQL instead, see Part F.) The same in the
   container image, whose entrypoint is `flyway` (`--rm` discards the licence cache with the container):

   ```bash
   docker run --rm -v "$PWD:/work" -w /work -v "$FLYWAY_SF_KEY_FILE:/keys/sf.p8:ro" \
     -e SNOWFLAKE_ACCOUNT -e FLYWAY_SF_USER -e FLYWAY_SF_ROLE -e FLYWAY_SF_WAREHOUSE \
     -e FLYWAY_SF_KEY_FILE=/keys/sf.p8 -e FLYWAY_SF_KEY_PWD -e FLYWAY_EMAIL -e FLYWAY_TOKEN \
     -e JAVA_ARGS='-Dnet.snowflake.jdbc.useBundledBouncyCastleForPrivateKeyDecryption=true' \
     redgate/flyway:13.8.1 -environment=DEV info
   ```

   A **manual migrate** (repairs, break-glass) should pass the deploy metadata the callbacks write to
   `FLYWAY.DEPLOY_LOG`, in the environment-scoped form. Every environment in `flyway.toml` has its own placeholders
   table, and that table **replaces** any root-level value (it is not merged), so a root-level
   `-placeholders.deploy_id=...` is silently ignored on every runway; `-environments.<ID>.flyway.placeholders.x=`
   reaches it. Without the flags the defaults from `flyway.toml` apply (`deploy_id = "manual"`,
   `git_sha = "unknown"`, ...), which leaves an anonymous row in the log:

   ```bash
   SHA=$(python3 -c 'import json; print(json.load(open("bundle-manifest.json"))["git_sha"])')
   flyway -environment=DEV migrate \
     -environments.DEV.flyway.placeholders.deploy_id="DEV-manual-$(date -u +%Y%m%d%H%M)" \
     -environments.DEV.flyway.placeholders.git_sha="$SHA" \
     -environments.DEV.flyway.placeholders.build_url=manual \
     -environments.DEV.flyway.placeholders.approver="<ticket approver>"
   ```

   When finished: unreserve the lock, delete `work/` (it holds the licence cache) and the key copy, close the shell.

**Verify:** `SHOW GRANTS OF ROLE EDW_<ID>_DEPLOYER;` lists only SYSADMIN and the service user for all six runways;
vault access to the six keys matches the table above; the Jenkins approval prompt of a test MGM run (H1) is offered
to edw-release-approvers members only.

**What can go wrong:** a stray `FLYWAY_*` variable in the shell (for example `FLYWAY_TARGET`) silently changes what
Flyway does; a manual run without a reserved lock can collide with a Jenkins deploy (nothing in Flyway prevents two
concurrent migrates); a manual migrate with `-placeholders.x=` instead of the environment-scoped form records
`manual` / `unknown` in `FLYWAY.DEPLOY_LOG` without any error; a session from outside the Jenkins egress range is
refused at login by the network policy; a key copy left on a host is a leaked deploy key (rotate it, RUNBOOKS R11).

---

## Part C - Repository and Jenkins setup

### C1. Copy the kit into the EDW repository

**Who:** platform engineer. **Where:** a workstation; Bitbucket.

**Do:**

```bash
git clone git@bitbucket.org:<workspace>/<edw-repo>.git && cd <edw-repo>
git switch -c platform/flyway-kit
cp -R /path/to/flyway-edw/. .                      # the kit root becomes the repository root
mkdir -p .bitbucket && mv bitbucket/CODEOWNERS .bitbucket/CODEOWNERS
rm -rf migrations/SALES migrations/FINANCE repeatable/SALES     # kit samples: they would create SALES/FINANCE
touch migrations/.gitkeep repeatable/.gitkeep                   # failOnMissingLocations=true needs the folders
printf 'dist/\nreports/\nflyway.user.toml\n*.user.toml\n__pycache__/\n' >> .gitignore
python3 scripts/fw.py guard
```

Then decide `runways.toml` `baseline_version` (D3) and set `[kit]` versions if you pin different ones (change
`flyway_version` and `flyway_image` together). If you keep the kit in a subfolder instead of the root, change the
Script Paths in `jenkins/jobs.groovy` and the working directories in both Jenkinsfiles.

**Upgrading an existing copy of the kit** (a later kit version into a repository that already deploys): bring in
the kit's own files (`callbacks/`, `bootstrap/`, `scripts/`, `jenkins/`, `docs/`) and keep your migrations,
repeatables and `runways.toml` / `flyway.toml` values. `callbacks/` must then hold exactly the new kit's files: a
callback the new version renamed must not survive under its old name, or Flyway runs both (this version runs the
cross-environment guard as `afterMigrate__50_cross_env_guard.sql`, before the task restore). Before the first deploy
of the new version to each runway, re-run bootstrap/20 PART 0 + 2 + 6 there (B3, kit upgrade): without the new
`FLYWAY.DEPLOY_TASK_STATE.ROOT_FQN` column every deploy stops in `beforeMigrate__20` with error -20121, with nothing
applied.

**Verify:** `fw.py guard` prints `0 error(s)`; open a PR for the branch (it becomes the first PR build in C4).

**What can go wrong:** leaving the samples in place deploys a SALES and a FINANCE schema to every runway; `cp`
overwrites same-named files such as an existing `README.md` (merge it by hand); a committed `flyway.user.toml`
would be auto-loaded by Flyway and could redirect connections (it is ignored above, and the deploy job runs only
from the verified bundle, which never contains it).

### C2. Prepare the Jenkins controller

**Who:** Jenkins admin. **Where:** Manage Jenkins; the controller's JCasC YAML and JVM options.

**Do:**

1. Install the plugins from A2.
2. Add the controller JVM option `-Dorg.jenkinsci.plugins.workflow.cps.DSL.UNSAFE_GROOVY_INTERPOLATION=fail`, so a
   pipeline that interpolates a secret into a shell string fails instead of only warning.
3. Set **Jenkins Location** to the public HTTPS FQDN. The Bitbucket plugin does not post statuses for an unset,
   `localhost` or dot-less URL.
4. Optional: configure a **Resource Root URL** (a second host name) if you want Flyway's HTML reports to render in
   Jenkins; Jenkins' default Content-Security-Policy blocks their inline script and style. Otherwise download them.
5. Merge `jenkins/casc-snippet.yaml` into your JCasC configuration and reload it: lockable resources
   `ALFA_EDW_DEV` ... `ALFA_EDW_PRD` plus `ALFA_EDW_RH_MGM` and `ALFA_EDW_RH_PRD`, and the Role Strategy roles and
   group assignments from B7. If your Lockable Resources version has "Allow ephemeral resources", switch it off so a
   misspelt lock name fails instead of silently creating a new lock.
6. Set the controller environment variables the jobs read (`casc-snippet.yaml` `globalNodeProperties`):
   `BITBUCKET_WORKSPACE` and `BITBUCKET_REPO` (filled from the JCasC placeholders `EDW_BITBUCKET_WORKSPACE` and
   `EDW_BITBUCKET_REPO`; used by the seed job and the `deploy-<ID>` statuses), `EDW_AGENT_LABEL` (label of the CI
   agents, default `docker`), `EDW_DEPLOY_AGENT_LABEL` (label of the deploy agents, A2 point 4; it must differ from
   `EDW_AGENT_LABEL`), and behind a proxy `EDW_JAVA_PROXY_ARGS`, `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` and
   `PIP_INDEX_URL` (upper case only; the Guard stage derives the lower-case names apt needs, A2 point 6). The agents
   must be able to pull the two `runways.toml` images:
   `flyway_image` (`redgate/flyway:13.8.1`, the exact version tag) and `python_image`, preferably the internal
   python + git + snowflake-cli image of A2 point 4. If you mirror images, point both keys at the mirror; either
   way, pin them by digest (`<image>:<tag>@sha256:<digest>`, A2 point 4).
7. **Keep the deploy agents for the deploy jobs.** Give the deploy nodes the `EDW_DEPLOY_AGENT_LABEL` label and
   restrict them on the node side to jobs whose full name matches `EDW/deploy/.*` (Job Restrictions plugin), or
   serve them from a separate cloud / agent template that the `EDW/ci` folder cannot use; ideally single-use agents
   for MGM and PRD. Never give a CI agent the deploy label, and never let the deploy nodes run CI builds.

**Verify:** Lockable Resources lists the eight resources; Role Strategy shows the AD groups; a user in
edw-developers can read `EDW/ci` but sees no Build button under `EDW/deploy/PRD`; a test branch whose Jenkinsfile
asks for `node('<deploy label>')` waits forever or is refused, and never runs on a deploy node.

**What can go wrong:** Role Strategy item roles need the parent folders readable too (`EDW`, `EDW/deploy`) or users
see nothing; a global role with Job/Build overrides every item restriction; deploy nodes without the node-side
restriction let any branch build run next to a PRD deploy (A2 point 4); behind a proxy, a python image without git
makes every CI build fail at Guard while `apt-get` cannot reach the Debian mirrors.

### C3. Seed the jobs and connect the webhook

**Who:** Jenkins admin, Bitbucket admin. **Where:** Jenkins; Bitbucket repository settings.

**Do:**

1. Check the settings at the top of `jenkins/jobs.groovy`: leave the workspace and repository empty to use the
   controller variables from C2, keep the credential IDs of B4, and set `kitDir` only if the kit is not the
   repository root. Merge any change to `main`.
2. Create an admin-only seed job (Freestyle: Source Code Management = the EDW repository, branch `main`; build step
   "Process Job DSLs", "Look on Filesystem" = `jenkins/jobs.groovy`). Run it; approve the script under In-process
   Script Approval if Jenkins asks.
3. It creates the folders `EDW`, `EDW/ci`, `EDW/deploy`, `EDW/deploy/<ID>`, the multibranch project
   `EDW/ci/edw-flyway` and the six `EDW/deploy/<ID>/deploy` jobs (it reads `runways.toml`, updates what it created
   and deletes nothing, so re-running it is safe). Now create the credentials (B4). Each deploy job takes its runway
   from its folder name; if you define a folder variable for it, call it `EDW_TARGET`, never `FLYWAY_TARGET`.
4. Webhook: either let Jenkins manage hooks (Bitbucket endpoint with "Manage hooks" and a credential allowed to
   manage webhooks), or add it by hand: URL `https://<jenkins>/bitbucket-scmsource-hook/notify`, events
   `repo:push`, `pullrequest:created`, `pullrequest:updated`, `pullrequest:fulfilled`, `pullrequest:rejected`.
   Configure a webhook secret and the matching Secret text credential for signature verification on the Jenkins
   Bitbucket endpoint, because the notify URL itself is unauthenticated.

**Verify:** the multibranch scan log finds `main`; each deploy job shows "Build with Parameters". If a deploy job
shows a plain "Build", run it once: declarative parameters register on the first run, which stops harmlessly at
"Fetch bundle" because `SOURCE_BUILD` is empty.

**What can go wrong:** the webhook cannot reach Jenkins (firewall: allow Atlassian's outgoing ranges, or rely on
periodic scans); the first main build must run before deploy jobs can copy its bundle (the main Jenkinsfile grants
`/EDW/deploy/*` copy-artifact permission).

### C4. First PR build

**Who:** platform engineer. **Where:** Bitbucket, Jenkins `EDW/ci/edw-flyway`.

**Do:** push the C1 branch and open a PR to `main` (or any small documentation PR).

**Verify:** the PR shows a Jenkins build status that turns green; the `PR-<id>` job ran Guard (JUnit results per FG
rule), the licence check, `flyway check -code` (report archived), and info/validate against DEV with the auditor
login, with the list of migrations that would be pending on DEV in the build description.

**What can go wrong:** before DEV is baselined (D5) the DEV stage finds no schema history: expected, re-run after
adoption; no status in Bitbucket (Jenkins URL, token scope); the Flyway container exits at once (the Jenkinsfiles
clear the image's `flyway` entrypoint with `--entrypoint=""`; keep that); licence stage fails (B6).

---

## Part D - Adopting the six existing databases (brownfield)

Order: practise on DEV, then PRD, then bring MGM and the test runways to PRD's state. Plan a change freeze for each
database while its ownership moves and it is baselined.

### D1. Plan the adoption

**Who:** lead DBA with the ETL owners. **Where:** Snowsight.

**Do:**

1. Inventory owners per database (repeat for each):

   ```sql
   SELECT TABLE_SCHEMA, TABLE_OWNER, TABLE_TYPE, COUNT(*) AS N
   FROM ALFA_EDW_PRD.INFORMATION_SCHEMA.TABLES
   WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA'
   GROUP BY 1, 2, 3
   ORDER BY 1, 2, 3;
   ```

2. Find processes that run DDL outside Flyway (they lose that ability once the deploy role owns the objects).
   `QUERY_HISTORY` lags up to about 45 minutes and records the session's database, so treat this as a starting
   point for a conversation with the ETL owners, not as proof:

   ```sql
   SELECT ROLE_NAME, USER_NAME, QUERY_TYPE, COUNT(*) AS N
   FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
   WHERE DATABASE_NAME = 'ALFA_EDW_PRD'
     AND START_TIME > DATEADD(DAY, -30, CURRENT_TIMESTAMP())
     AND (QUERY_TYPE LIKE 'CREATE%' OR QUERY_TYPE LIKE 'ALTER%' OR QUERY_TYPE LIKE 'DROP%'
          OR QUERY_TYPE LIKE 'RENAME%')
   GROUP BY 1, 2, 3
   ORDER BY 4 DESC;
   ```

3. For each such process decide: rewrite it (e.g. TRUNCATE + INSERT instead of CREATE OR REPLACE), move its scratch
   objects to names covered by `runways.toml` `[drift] ignore_name_patterns`, or list its service user in
   `allowed_ddl_users`. Keep those lists short; each entry is a reviewed PR.
4. List objects that clones handle specially: hybrid tables, external tables, pipes on internal stages, and masking
   or row access policies stored outside the database (A1, decision 6).

**Verify:** a signed adoption plan per database: freeze window, DDL-issuing processes and their fix, rollback contact.

**What can go wrong:** an ETL job that recreates its tables fails the morning after D2; plan its fix before the freeze.

### D2. Transfer ownership to the deploy role (dry run, then apply)

**Who:** Snowflake admin (SYSADMIN). **Where:** Snowsight, `bootstrap/20_runway_database.sql` PART 3 (brownfield
adoption).

**Do:**

1. Run PART 0 + 1 + 2 first (B3): the apply records the started tasks in `FLYWAY.DEPLOY_TASK_STATE`, which PART 2
   creates and upgrades. In PART 0 keep `dry_run = TRUE` and settle two switches:
   * `keep_previous_owner_access` (default TRUE). A role that owned schemas or objects loses its implicit owner rights
     at the transfer: COPY CURRENT GRANTS keeps the grants to OTHER roles, not the owner's own. TRUE plans working
     grants for each previous owner, per schema: USAGE on the schema; SELECT, INSERT, UPDATE, DELETE, TRUNCATE on all
     tables; SELECT on all views and streams; USAGE on all sequences, file formats, functions and procedures.
     Stages, dynamic and external tables and task operation (OPERATE / MONITOR) are not covered: grant those by hand
     from the dry-run list. FALSE plans none.
   * `include_governance_objects` (default FALSE). Masking and row access policies and tags stay with their current
     owner (normally a governance role: separation of duties), and migrations must then not replace them. TRUE moves
     them to the deploy role too, only where no governance role owns them on purpose. Secrets are never moved: their
     owner grants USAGE on them to the deploy role where a procedure or integration of the runway needs them.
2. Run PART 3 (the dry run executes nothing). Before the plan, the report lists every started task and every started
   alert (fully quoted), every pipe that moves with its execution state now, each previous owner role with the
   schemas it owned, and WARNING / NOTE lines (future OWNERSHIP grants to another role, serverless tasks, alerts). The
   plan, in order: USAGE on every warehouse a task, dynamic table or alert names (they then run as the deploy role);
   then per schema (FLYWAY and PUBLIC included) the moving pipes paused, `GRANT OWNERSHIP ON SCHEMA` and
   `ON ALL <type> IN SCHEMA ... COPY CURRENT GRANTS` (alerts and tasks only in schemas that still hold one the deploy
   role does not own), each pipe moved on its own (Snowflake refuses bulk ownership grants on pipes and moves a pipe
   only while it is paused) and force-resumed with `SYSTEM$PIPE_FORCE_RESUME` if it was RUNNING, the previous owners'
   working grants, and `ALTER SCHEMA ... ENABLE MANAGED ACCESS`. Everything moves schema-wide, never task by task.
3. Review it with the D1 inventory (every schema, every object type) and clear the warnings first: revoke or re-point
   FUTURE OWNERSHIP grants to another role (or Flyway-created objects get the wrong owner); where the database holds
   alerts, grant the deploy role `EXECUTE ALERT` (B2 step 3; `EXECUTE MANAGED ALERT` for serverless ones), and
   `EXECUTE MANAGED TASK` for serverless tasks.
4. In the freeze window, `SET dry_run = FALSE;` and run PART 3 again. It refuses to start, changing nothing, when PART
   0's names disagree, `SHOW TASKS` failed (the started tasks would be unknown), or the database holds alerts and the
   deploy role lacks `EXECUTE ALERT` (or `EXECUTE MANAGED ALERT` for serverless ones). It then records every started
   task in `FLYWAY.DEPLOY_TASK_STATE` as `DEPLOY_ID = 'ADOPT-<timestamp>'` (printed in the report) **before the first
   transfer**, and applies nothing if it cannot. It runs the plan one statement at a time (a failure is reported and
   the rest continue), then resumes every task that was started before and is not started now (children, then
   finalizers, then roots) and every alert that was started before. A transfer of a schema's tasks **always**
   suspends all of them, even to the role that already owns them, so this resume is part of the apply, not an
   option; it is also why PART 3 moves tasks only where one is not owned yet, which keeps a re-run safe.
5. Read every `-- FAILED` line. On `ALL <type>`: an object of that type is owned by a role outside SYSADMIN's
   hierarchy; have SECURITYADMIN grant that role to SYSADMIN and run PART 3 again (never for a governance,
   policy-admin or secrets-owning role: keep those objects with their owner). On `ENABLE MANAGED ACCESS`: SECURITYADMIN
   runs that line (verify on DEV first whether SYSADMIN may). A previous owner that is a DATABASE role gets FAILED
   working grants: grant them by hand `TO DATABASE ROLE`. The optional ACCOUNT_USAGE query after the block
   (ACCOUNTADMIN, about two hours behind) lists owners SYSADMIN cannot see.
6. Objects now owned by the deploy role also **execute** as the deploy role: owner's-rights procedures, tasks, pipes
   and secure views. Grant `EDW_<ID>_DEPLOYER` whatever they read or use outside the database (landing databases,
   storage or API integrations, other warehouses) before re-enabling the ETL.
7. Managed access (step 2's last statement per schema) lets the deploy role, as schema owner, define the layer-90
   future grants (B3). Existing grants stay; from now on only the schema owner grants on objects in the schema, so
   access requests go through the schema's grants file.

Do DEV first as the rehearsal of the procedure, then PRD. MGM, SAN, SEA and PHX either repeat this step in place or
are re-cloned from PRD in D6 (bootstrap/30 hands a clone over to the runway's role).

**Verify:** bootstrap/20 PART 6 reports every schema owned by `EDW_<ID>_DEPLOYER` and MANAGED ACCESS, and every task
owned by it; by hand:

```sql
SHOW SCHEMAS IN DATABASE ALFA_EDW_PRD;
SELECT "name", "owner", "options" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
WHERE "name" <> 'INFORMATION_SCHEMA'
  AND ("owner" <> 'EDW_PRD_DEPLOYER' OR COALESCE("options", '') NOT LIKE '%MANAGED ACCESS%');
-- expect no rows; then re-run the D1 owner inventory: one owner, EDW_PRD_DEPLOYER
SHOW TASKS IN DATABASE ALFA_EDW_PRD;
SELECT "schema_name", "name", "owner", "state" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) ORDER BY 1, 2;
-- every task the dry run listed as started is started again
```

**What can go wrong:** the report starts "APPLIED with the RESUME PHASE FAILED", or a resume line says FAILED: the
transfer is done but tasks or alerts stayed suspended; resume them by hand from the report or the `ADOPT-` rows
(RUNBOOKS R9) once the cause is fixed (`EXECUTE TASK`, `EXECUTE ALERT`, warehouse USAGE on the deploy role);
"NOT APPLIED: the started tasks could not be recorded": run PART 2, then PART 3 again; an ETL role that needs more
than the working grants (stages, task OPERATE) fails until granted; a pipe whose state read UNKNOWN stays paused
(resume it deliberately); ownership moved task by task would sever task-graph links, which is why PART 3 never does
it: never "fix" one task with `GRANT OWNERSHIP`.

### D3. Choose and record the baseline version

**Who:** lead DBA. **Where:** `runways.toml` (PR).

**Do:** `[kit] baseline_version` (example `2026.10.01.0000`) is the version `flyway baseline` writes into every
adopted database; everything at or below it counts as already delivered. Choose a timestamp:

* earlier than every `V` file you will ever merge (at adoption `migrations/` is empty, so "before the first
  migration is written" is enough);
* identical for all six databases;
* fixed for good once the first database is baselined.

**Verify:** `python3 scripts/fw.py env --runway PRD --format json` shows `EDW_BASELINE_VERSION`; guard is clean.

**What can go wrong:** a `V` file at or below the baseline shows as "Below Baseline" and never runs anywhere.

### D4. (Optional) Capture PRD's starting state as code

**Who:** DBA. **Where:** a workstation with the Snowflake CLI and your own admin login for the GET_DDL extract; a
B7 step 4 session for the Enterprise route (it connects with the PRD service login).

Not needed for adoption, but useful: a reviewed record of what PRD looked like on day one, the reference for D6, and
the source when someone first changes an object that predates Flyway (E1, tip 4). Keep it **outside** Flyway's
locations, e.g. `baseline/ALFA_EDW_PRD/<SCHEMA>.sql`: the six databases are adopted with `flyway baseline`, so a `B`
script would never run on them, and fresh runways are built by clone (bootstrap/30). Two ways:

* **GET_DDL extract** (works today), one file per schema, run as a role that sees everything (SYSADMIN inherits the
  deploy roles):

  ```bash
  for S in $(snow sql -c admin --format json -q "SELECT SCHEMA_NAME FROM ALFA_EDW_PRD.INFORMATION_SCHEMA.SCHEMATA
             WHERE SCHEMA_NAME NOT IN ('INFORMATION_SCHEMA', 'PUBLIC', 'FLYWAY') ORDER BY 1" \
             | python3 -c 'import json,sys; print(" ".join(r["SCHEMA_NAME"] for r in json.load(sys.stdin)))'); do
    mkdir -p baseline/ALFA_EDW_PRD
    snow sql -c admin --format json -q "SELECT GET_DDL('SCHEMA', 'ALFA_EDW_PRD.$S', TRUE) AS DDL" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["DDL"])' > "baseline/ALFA_EDW_PRD/$S.sql"
  done
  ```

* **Enterprise `diff` + `generate`** (comparison features are PREVIEW on Snowflake; verify the output on first run).
  The project lists only `FLYWAY` in `schemas`, so pass the EDW schemas explicitly:

  ```bash
  flyway -environment=PRD -schemas="<SCHEMA1>,<SCHEMA2>,..." diff generate -diff.source=PRD -diff.target=empty \
    -generate.types=baseline -generate.baselineFilename=baseline/B2026.10.01.0000__alfa_edw_prd.sql
  ```

**Verify:** the files open, contain every schema, and contain no credentials (guard FG017 scans all `.sql` files).

**What can go wrong:** extracts are large; a `B` script placed under `migrations/_baseline/` must pass every guard
rule and would still never run on an adopted database. Building a brand-new database from code is not part of this
kit's procedure; prove it on a scratch database first if you need it.

### D5. Baseline ALFA_EDW_PRD (then DEV)

**Who:** for PRD a break-glass operator (B7: ticket, reserved lock `ALFA_EDW_PRD`, PRD key from the vault); for DEV
an edw-dba member with the DEV key. **Where:** a host inside the Jenkins egress range (B7 step 4), Snowsight.

**Do:** from the latest main bundle (B7 step 4), or a clean checkout of `main` if no bundle exists yet:

```bash
eval "$(python3 scripts/fw.py env --runway PRD --format shell)"
# + the account/key/licence exports from B7 step 4
flyway -environment=PRD info                      # expect: no schema history yet, nothing applied
flyway -environment=PRD baseline -baselineVersion="$EDW_BASELINE_VERSION" \
  -baselineDescription="ALFA EDW adoption baseline"
flyway -environment=PRD info                      # expect: one Baseline row at the baseline version
```

Then run bootstrap/20's post-baseline section (SELECT on `FLYWAY."flyway_schema_history"` for
`EDW_FLYWAY_AUDITOR`; the table exists only now). Repeat for DEV after D6's DEV decision.

**Verify:**

```sql
SELECT "installed_rank", "version", "description", "type", "installed_by", "success"
FROM ALFA_EDW_PRD.FLYWAY."flyway_schema_history"
ORDER BY "installed_rank";
-- one row: type BASELINE, version = baseline_version, success = TRUE
```

and, with the auditor login, `fw.py env --runway PRD --auditor` + `flyway -environment=PRD info` works.

**A brand-new runway database** (not one of the six adopted ones) is prepared the same way, because the deploy job
never migrates a database without a schema version and has no switch to skip that check: run bootstrap/20 (B3),
then, from an R0 session, `flyway -environment=<ID> baseline -baselineVersion="$EDW_BASELINE_VERSION"` (the kit's
baseline_version is below every V file, so every V then applies), run bootstrap/20's post-baseline section, and
deploy normally. The usual way to fill a new test runway is still a clone of PRD (D6), which arrives baselined;
building a database from migrations alone is unproven here (D4).

**What can go wrong:** never run `migrate` before `baseline` (B3's safety net and the deploy job's schema-version
check both stop it, but a manual run with other settings would not);
baselining twice with different versions makes runways disagree (fix with RUNBOOKS before any deploy); the history
table is case-sensitive: always quote `"flyway_schema_history"` and its lower-case columns.

### D6. Bring MGM, SAN, SEA, PHX and DEV to PRD's state

**Who:** DBA (with the data owner's sign-off from A1). **Where:** Snowsight.

**Do:** for each database choose one route.

* **Refresh by clone (recommended for MGM and the test runways):** reserve the target's lock and, for the clone
  part (bootstrap/30 PART 3), `ALFA_EDW_PRD` as well, so a PRD deploy cannot run while PRD is being cloned and
  leave the clone half-applied. Then run `bootstrap/30_refresh_runway.sql` with the target runway (RUNBOOKS R8 walks
  through its parts). It runs its pre-checks (dangling policy references, hybrid and external tables, internal-stage
  pipes, `ALFA_EDW_PRD.` references inside PRD code), parks the current runway as `ALFA_EDW_<ID>_PRE_REFRESH` with
  its schedules paused, clones PRD as `EDW_PRD_DEPLOYER` (it owns the tasks, pipes and streams, which a clone only
  includes for their owner), hands every schema to `EDW_<ID>_DEPLOYER`, re-applies database-level grants and lists
  the suspended tasks, alerts, dynamic tables and pipes. The clone carries PRD's `FLYWAY` schema, so the runway is
  already baselined at PRD's state. MGM is accepted only when the script's explicit MGM switch is set. The first
  deploy afterwards reports **every cloned object as drift** (LAST_DDL is the clone time, LAST_DDL_BY the admin who
  cloned): review the list once and, where the runway's `drift_gate` is `fail`, re-run that deploy with
  `ACK_DRIFT = true`, citing the refresh ticket (RUNBOOKS R8).
* **Reconcile in place (DEV, or a database you cannot clone over):** compare it with PRD, turn real differences
  into migrations or drop them, then baseline it exactly as D5. Run as SYSADMIN:

  ```sql
  -- columns that exist in only one database or differ in type (tables and views)
  WITH p AS (
      SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, IS_NULLABLE,
             DATA_TYPE || '(' || COALESCE(TO_VARCHAR(CHARACTER_MAXIMUM_LENGTH),
                                          TO_VARCHAR(NUMERIC_PRECISION) || ',' || TO_VARCHAR(NUMERIC_SCALE), '')
                       || ')' AS TYPE_SIG
      FROM ALFA_EDW_PRD.INFORMATION_SCHEMA.COLUMNS
      WHERE TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA', 'FLYWAY')
  ), d AS (
      SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, IS_NULLABLE,
             DATA_TYPE || '(' || COALESCE(TO_VARCHAR(CHARACTER_MAXIMUM_LENGTH),
                                          TO_VARCHAR(NUMERIC_PRECISION) || ',' || TO_VARCHAR(NUMERIC_SCALE), '')
                       || ')' AS TYPE_SIG
      FROM ALFA_EDW_DEV.INFORMATION_SCHEMA.COLUMNS
      WHERE TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA', 'FLYWAY')
  )
  SELECT COALESCE(p.TABLE_SCHEMA, d.TABLE_SCHEMA) AS SCHEMA_NAME,
         COALESCE(p.TABLE_NAME, d.TABLE_NAME) AS OBJECT_NAME,
         COALESCE(p.COLUMN_NAME, d.COLUMN_NAME) AS COLUMN_NAME,
         p.TYPE_SIG AS PRD_TYPE, d.TYPE_SIG AS DEV_TYPE
  FROM p
  FULL OUTER JOIN d
    ON d.TABLE_SCHEMA = p.TABLE_SCHEMA AND d.TABLE_NAME = p.TABLE_NAME AND d.COLUMN_NAME = p.COLUMN_NAME
  WHERE p.COLUMN_NAME IS NULL OR d.COLUMN_NAME IS NULL
     OR p.TYPE_SIG <> d.TYPE_SIG OR p.IS_NULLABLE <> d.IS_NULLABLE
  ORDER BY 1, 2, 3;
  ```

  ```sql
  -- views, functions and procedures that exist in one database only or whose definition differs
  WITH defs AS (
      SELECT 'PRD' AS DB, 'VIEW' AS KIND, TABLE_SCHEMA AS S, TABLE_NAME AS N, VIEW_DEFINITION AS DEF
      FROM ALFA_EDW_PRD.INFORMATION_SCHEMA.VIEWS WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA'
      UNION ALL
      SELECT 'DEV', 'VIEW', TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION
      FROM ALFA_EDW_DEV.INFORMATION_SCHEMA.VIEWS WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA'
      UNION ALL
      SELECT 'PRD', 'FUNCTION', FUNCTION_SCHEMA, FUNCTION_NAME || ARGUMENT_SIGNATURE, FUNCTION_DEFINITION
      FROM ALFA_EDW_PRD.INFORMATION_SCHEMA.FUNCTIONS
      UNION ALL
      SELECT 'DEV', 'FUNCTION', FUNCTION_SCHEMA, FUNCTION_NAME || ARGUMENT_SIGNATURE, FUNCTION_DEFINITION
      FROM ALFA_EDW_DEV.INFORMATION_SCHEMA.FUNCTIONS
      UNION ALL
      SELECT 'PRD', 'PROCEDURE', PROCEDURE_SCHEMA, PROCEDURE_NAME || ARGUMENT_SIGNATURE, PROCEDURE_DEFINITION
      FROM ALFA_EDW_PRD.INFORMATION_SCHEMA.PROCEDURES
      UNION ALL
      SELECT 'DEV', 'PROCEDURE', PROCEDURE_SCHEMA, PROCEDURE_NAME || ARGUMENT_SIGNATURE, PROCEDURE_DEFINITION
      FROM ALFA_EDW_DEV.INFORMATION_SCHEMA.PROCEDURES
  )
  SELECT KIND, S AS SCHEMA_NAME, N AS OBJECT_NAME,
         CASE WHEN COUNT_IF(DB = 'PRD') = 0 THEN 'only in DEV'
              WHEN COUNT_IF(DB = 'DEV') = 0 THEN 'only in PRD'
              ELSE 'definition differs' END AS FINDING
  FROM (SELECT DB, KIND, S, N,
               MD5(REPLACE(UPPER(COALESCE(DEF, '')), 'ALFA_EDW_DEV', 'ALFA_EDW_PRD')) AS H
        FROM defs)
  GROUP BY KIND, S, N
  HAVING COUNT(DISTINCT DB) = 1 OR COUNT(DISTINCT H) > 1
  ORDER BY 1, 2, 3;
  ```

  Enterprise `flyway diff -diff.source=PRD -diff.target=DEV` can produce the same picture (preview on Snowflake;
  both environments then need one login that can read both databases; verify on first run).

**Verify:** each refreshed runway's `flyway info` (auditor) shows PRD's Baseline row; each reconciled database shows
its own Baseline row at the same version; the two queries return only differences you have decided to keep.

**What can go wrong:** refreshed runways start with tasks, alerts, dynamic tables and pipes suspended and with stream
offsets at the clone point; a PRD view that names `ALFA_EDW_PRD.` still reads PRD from the runway (bootstrap/30
lists them; the first deploy's cross-environment guard then fails until they are fixed, RUNBOOKS R10); PRD's
functional-role grants come along on child objects (bootstrap/30 re-grants them to runway roles).

### D7. Verify adoption and hand over to the pipeline

**Who:** lead DBA, release manager. **Where:** Jenkins, Snowsight.

**Do:**

1. For every runway: the H1 checklist up to the dry run (`DRY_RUN = true` deploy of the current main build).
2. Repeatables: the first real deploy runs **every** `R__` file in the repository (none has run yet) and replaces
   those objects. At adoption, `repeatable/` must be empty or hold exactly PRD's current definitions, reviewed.
3. Announce go-live: from now on DDL in MGM/PRD happens only through the pipeline; the drift check reports the rest.

**Verify:** six green dry runs; `SHOW GRANTS OF ROLE` audits from B7; the freeze lifted.

**What can go wrong:** a forgotten DDL-issuing process (D1) shows up as drift at the first deploy: fix the process or
add it to `allowed_ddl_users` by PR. On a runway refreshed by clone (D6) the first dry run and deploy also list every
cloned object as drift (clone time, cloning admin); that is expected once: review it and acknowledge it with
`ACK_DRIFT = true` where `drift_gate` is `fail`, citing the refresh ticket.

---

## Part E - Day-to-day developer workflow

### E1. Branch, scaffold, write, guard, pull request

**Who:** data engineer. **Where:** workstation (Python 3.11+, git), Bitbucket, Jenkins.

**Do:**

```bash
git fetch origin && git switch -c feature/EDW-1234-order-status origin/main
python3 scripts/fw.py new --schema SALES --desc add_order_status           # versioned (tables, data)
#   -> migrations/SALES/V2026.10.14.0912__sales_add_order_status.sql   (UTC timestamp version)
# python3 scripts/fw.py new --schema SALES --desc add_order_status --undo  # INSTEAD of the line above: V + U pair
python3 scripts/fw.py new --schema SALES --repeatable --layer 30 --object v_open_orders   # new view
# edit the files following docs/CONVENTIONS.md, then:
python3 scripts/fw.py guard --base origin/main
git add -A && git commit -m "EDW-1234 SALES: add ORDER_STATUS" && git push -u origin HEAD
```

Every push of a branch without a pull request builds that branch: guard, code review and a **branch bundle** you can
deploy to DEV or to your leased runway (F1, F2). Then open a pull request to `main`. Jenkins runs Guard (against
`origin/main`), the licence check, `flyway check -code`, and `info`/`validate` of the merge result against DEV with
the read-only login. Reviewers: CODEOWNERS plus the branch-restriction approvals. After the merge, the `main` build
bundles the change and deploys it to DEV automatically (F1); only `main` triggers a deploy by itself.

Tips:

1. Tables and data: versioned files only (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`,
   idempotent `MERGE`). Views, functions, procedures, tasks, grants: edit the repeatable file in place; `fw.py new
   --repeatable` refuses to create one that already exists. Fill in every `<...>` marker the scaffold wrote: FG021
   fails the file until you do.
2. One logical change per versioned file: on Snowflake Flyway runs every statement in autocommit, DML included, so a
   failed file is half applied. DML that must be atomic is one `MERGE` or one explicit transaction (CONVENTIONS 5.3).
3. Never edit or rename a migration that is on `main`; write a new one (guard FG003 fails the PR otherwise).
4. Changing an object that predates adoption: first add its current definition (from D4's extract or `GET_DDL`) as
   the repeatable file in its own PR, then change it in a second PR, so reviewers see the real diff.

**Verify:** the PR's build status is green; the guard JUnit view shows every FG rule passing; after the merge the DEV
deploy (F1) is green.

**What can go wrong (guard findings):**

| Rule  | Typical cause                                   | Fix                                             |
|-------|-------------------------------------------------|-------------------------------------------------|
| FG001 | hand-typed file name, wrong folder              | re-create it with `fw.py new`                   |
| FG002 | two people scaffolded in the same minute        | re-scaffold with `fw.py new`; delete the old    |
| FG003 | edited an applied migration                     | revert it; write a new migration                |
| FG004 | warning: older than main's newest version       | fine if independent (runs out of order)         |
| FG005 | `${...}` undeclared (comments too), `${ x }`    | declare it in flyway.toml (JavaScript: below)   |
| FG006 | DROP / TRUNCATE / CREATE OR REPLACE TABLE       | ALTER instead, or a waiver with a ticket        |
| FG008 | `USE ...` / `ALTER SESSION`                     | schema-qualify instead                          |
| FG009 | `ALFA_EDW_...` in code; any `${home_db}`        | `SCHEMA.OBJECT`; never name the database        |
| FG010 | unqualified CREATE target                       | `SCHEMA.OBJECT` (it would land in FLYWAY)       |
| FG011 | CREATE OR REPLACE view/proc without COPY GRANTS | add `COPY GRANTS`                               |
| FG011 | warning: schema without managed access          | `CREATE SCHEMA ... WITH MANAGED ACCESS`         |
| FG012 | table DDL or DML in a repeatable                | move it to a V script                           |
| FG013 | repeatable uses one that runs later             | raise its layer number (e.g. 30 -> 31)          |
| FG014 | task change without gate and resume             | start from the layer-80 template of `fw.py new` |
| FG015 | bare `BEGIN` / `DECLARE` block, or a bare `IF`  | wrap it in `EXECUTE IMMEDIATE $$ ... $$;`       |
| FG021 | `<JIRA-KEY>`, `<TABLE>`, ... left unfilled      | fill in every scaffold marker                   |

FG004 is a warning: the migration will apply out of order wherever newer versions already ran; if it depends on
them, re-scaffold it (renaming is fine until it reaches `main`). FG005 in a JavaScript UDF whose body uses `${...}`
template literals: add `<file>.sql.conf` with `placeholderReplacement=false` (CONVENTIONS.md). FG015's bare `IF`:
`CREATE ALERT ... IF (EXISTS ...)`, `ALTER TABLE ... ADD IF NOT EXISTS` without COLUMN, `ALTER/DROP ALERT IF EXISTS`
and `CREATE SECRET IF NOT EXISTS` stop Flyway's parser with "Incomplete statement"; write `ADD COLUMN IF NOT EXISTS`,
or wrap the statement (`fw.py new --layer 85` writes alerts that way). FG013's layer bump renames a file that may
already have run; that is safe, and a retired repeatable also needs a V script that drops its object (CONVENTIONS
5.15). FG006, FG007 and FG008 also look inside `EXECUTE IMMEDIATE $$ ... $$` and `EXECUTE IMMEDIATE '...'`.

A waiver is one line in the file: `-- FLYWAY-WAIVER: FG006 EDW-1234 drop retired staging table after archive`
(rule, ticket, reason). `DROP DATABASE`, `CREATE OR REPLACE DATABASE/SCHEMA`, `USE DATABASE/SCHEMA/ROLE`,
`${home_db}` in a migration and an unfilled scaffold marker (FG021) cannot be waived. A waived file passes code
review: the guard is the gate for every waivable rule, and the code-review rules that repeat one (ALFA03 for
CREATE OR REPLACE TABLE and DROP SCHEMA, ALFA06 for account-level statements, RG06, RG13, RG08, RG11) are warnings
that list the statement for the reviewers. An intended `UPDATE` without `WHERE` has no guard rule but fails code
review (RG09): end its line with `-- noqa: RG09` (CONVENTIONS section 8).

---

## Part F - Deploying

Where to see what is deployed: each database's `FLYWAY."flyway_schema_history"` and `FLYWAY.DEPLOY_LOG`, the
deploy jobs' history, and the `deploy-<ID>` commit statuses in Bitbucket (Bitbucket's own Deployments view is a
Bitbucket Pipelines feature and is not fed by Jenkins).

```sql
SELECT DEPLOY_ID, EVENT, ENV, GIT_SHA, APPROVER, FLYWAY_USER, EVENT_TS, DETAIL
FROM ALFA_EDW_PRD.FLYWAY.DEPLOY_LOG
ORDER BY EVENT_TS DESC
LIMIT 20;
```

### F1. DEV

**Who:** nobody for the normal path; a DBA or lead for a branch deploy. **Where:** Jenkins.

**Do:**

* **Automatic:** a merge to `main` starts build N of `EDW/ci/edw-flyway/main`: guard, code review, bundle
  (`edw-flyway-bundle-N.tar.gz` + `bundle-manifest.json`, archived with fingerprints), then it triggers
  `EDW/deploy/DEV/deploy` with `SOURCE_BUILD = N`.
* **Branch deploy (project work not merged yet):** on `EDW/deploy/DEV/deploy` choose Build with Parameters:
  `SOURCE_JOB = /EDW/ci/edw-flyway/<branch job>`, `SOURCE_BUILD = <that job's build number>`. Plain branch builds
  archive a bundle; `PR-<id>` builds never do, and once a branch has an open pull request Jenkins builds it only as
  `PR-<id>`. So deploy a branch bundle before opening the PR (or keep a long-lived project branch without one).
  Use the branch job's name exactly as Jenkins lists it under `EDW/ci/edw-flyway` (names with `/` are encoded).
  DEV, SAN, SEA and PHX accept the `branch` lane; MGM and PRD never do.

**Verify:** the deploy job is green; Bitbucket shows `deploy-DEV` on the commit (main lane); `FLYWAY.DEPLOY_LOG` has
STARTED and SUCCEEDED rows with `DEPLOY_ID = 'DEV-<build>'`; `flyway info` (auditor) shows the new versions as
Success.

**What can go wrong:** a failed migration (RUNBOOKS R1); DEV keeps `tasks_enabled = false`, so the deploy suspends
every started root task in DEV, including ones somebody resumed by hand; drift warnings from people working directly
in DEV (reconcile them, they will block nothing in DEV but they hide real problems); a branch deploy to DEV is
overwritten by the next main deploy for any repeatable both touch.

### F2. Test runways SAN, SEA, PHX

**Who:** the runway's lease lead (edw-runway-leads-<ID>), DBA for refreshes. **Where:** Jenkins, Snowsight.

**Do:**

1. **Lease.** One project per runway at a time, recorded in a ticket (project, lead, start/end, lanes used). Add the
   lead to edw-runway-leads-<ID>. To require an approval on that runway, set its `approval_group` in `runways.toml`.
2. **Start of lease:** refresh the runway from PRD (`bootstrap/30_refresh_runway.sql`, RUNBOOKS R8) unless the
   project accepts the previous state. A refresh resets the runway to PRD, including the Flyway history, so the
   project then deploys the current main bundle and its own branch bundle again.
3. **Deploy:** `EDW/deploy/<ID>/deploy`, Build with Parameters: `SOURCE_JOB` = main (default) or the project's branch
   job, `SOURCE_BUILD = N`. The **promotion check** requires that the bundle already ran cleanly on DEV: every
   versioned migration applied, every repeatable at the same revision (not Outdated).
4. **Tasks:** `tasks_enabled` is `false` on test runways. A project that needs its task graphs running changes the
   runway's value in `flyway.toml` by PR; that changes the checksum of every repeatable using `${tasks_enabled}` in
   that runway, so they all re-run at the next deploy there. Remember the account-wide limit of resumed tasks
   (10,000 per Snowflake's documentation) and the warehouse cost.
5. **End of lease:** refresh the runway (R8) so the next project does not inherit unmerged objects.

**Verify:** the console shows `promote-check <ID>: PASS (needs any one of DEV)`; `deploy-<ID>` status (main lane);
DEPLOY_LOG rows.

**What can go wrong:** promote-check FAIL because DEV has moved on (a newer main deploy changed a repeatable): deploy
a newer bundle; a project changed a migration that already ran on its runway: checksum mismatch, RUNBOOKS R2;
unmerged migrations of the previous project stay in the runway (tolerated as `*:missing`, but the objects are real):
refresh between projects.

### F3. MGM (preproduction)

**Who:** release manager (starts), a member of edw-release-approvers (approves). **Where:** `EDW/deploy/MGM/deploy`.

**Do:**

1. **Pick the release candidate:** a main build N that is green on at least one of SAN, SEA, PHX (see the
   `deploy-SAN/SEA/PHX` statuses on its commit). Its `bundle-manifest.json` lists exactly what ships.
2. **Rehearse without touching MGM:** Build with Parameters `SOURCE_BUILD = N`, `DRY_RUN = true`. The job verifies
   the bundle is from main, checks the licence, runs the promotion check, pre-flight and drift, writes the dry-run
   report (`flyway check -dryrun`: the exact SQL Flyway would run), clones MGM to `ALFA_EDW_RH_MGM`, migrates the
   clone with tasks off, and drops the clone.
3. **Review:** the dry-run report, the rehearsal log, the drift output, any out-of-order migrations (versions older
   than ones already applied: allowed, but read them), and the waivers in the release
   (`grep -rn FLYWAY-WAIVER migrations repeatable`). The rehearsal runs **more repeatables than MGM will**: the
   rehearsal environment sets `tasks_enabled = "false"` and `edw_db = ALFA_EDW_RH_MGM`, and repeatable checksums
   include placeholder values, so every repeatable that uses `${tasks_enabled}` or `${edw_db}` (every task file,
   for a start) is Outdated on the clone and re-runs there, changed or not. What MGM itself will run is the
   pre-flight `flyway info` pending list and the dry-run report. A rehearsal failure in such an unchanged file still
   needs a fix forward: the same file re-runs on every test runway refreshed from PRD (SHORTCOMINGS S09).
4. **Deploy:** same parameters with `DRY_RUN = false`. The job repeats the checks and the rehearsal, then pauses for
   approval; an edw-release-approvers member clicks Proceed (the prompt times out after a day). It records the Time
   Travel point, migrates MGM, restores task graphs (`tasks_enabled = true`), archives `flyway info` and posts
   `deploy-MGM`.

**Verify:** DEPLOY_LOG SUCCEEDED with `MGM-<build>` (a TASKS_RESUMED row lists the graphs the restore had to fix);
`SHOW TASKS IN DATABASE ALFA_EDW_MGM` shows every task that was started before the deploy, root or member, started
again; release smoke tests pass on MGM.

**What can go wrong:** promote-check FAIL (the candidate never completed on a test runway); verify-bundle refuses a
branch bundle; the rehearsal fails: fix forward on main and build a new candidate; drift reported (`warn` prints it;
reconcile before PRD, RUNBOOKS R5); a rehearsal clone left behind after an aborted job (RUNBOOKS R12).

### F4. PRD (production)

**Who:** release manager (starts), a member of edw-prod-approvers who is not the starter (approves), on-call DBA.
**Where:** `EDW/deploy/PRD/deploy`, Snowsight.

**Do:**

1. **Before the window:** approved change ticket; candidate = the build N already on MGM (PRD promotes from MGM
   only); release notes with smoke tests and a rollback note per change (F6); Time Travel retention on PRD covers the
   verification window (`SHOW PARAMETERS LIKE 'DATA_RETENTION_TIME_IN_DAYS' IN DATABASE ALFA_EDW_PRD;`; the maximum
   depends on your Snowflake edition, verify); optionally a `DRY_RUN = true` run the day before.
2. **Start:** Build with Parameters: `SOURCE_BUILD = N`, `CHANGE_TICKET = CHG-nnnn`, `DRY_RUN = false`,
   `ACK_DRIFT = false`.
3. **The job:** main-only bundle check, licence, promotion check against MGM, lock, pre-flight, drift gate, dry-run
   report, rehearsal on `ALFA_EDW_RH_PRD`, then the approval prompt.
4. **Approve (four-eyes):** the approver reads the dry-run report, rehearsal result, drift output and ticket number,
   then clicks Proceed. The job refuses an approval by the person who started it. As on MGM (F3 step 3), the
   rehearsal on `ALFA_EDW_RH_PRD` re-runs every `${tasks_enabled}` / `${edw_db}` repeatable; the dry-run report and
   the pre-flight pending list show what PRD itself will run.
5. **The job continues:** records the Time Travel point (UTC timestamp in the console and the DEPLOY_LOG STARTED
   row), migrates PRD, restores task graphs, archives `flyway info`, posts `deploy-PRD`.
6. **Post-checks (DBA):** DEPLOY_LOG SUCCEEDED row; task graphs back in their previous state (every root and member
   that was started before; one an operator had suspended stays suspended); release smoke tests; every changed view
   still compiles (`SELECT * FROM <SCHEMA>.<VIEW> LIMIT 0;`); write the Time Travel point into the ticket and close
   it.

**Verify:**

```sql
SELECT "installed_rank", "version", "description", "type", "success", "installed_on"
FROM ALFA_EDW_PRD.FLYWAY."flyway_schema_history"
ORDER BY "installed_rank" DESC
LIMIT 20;                                          -- this release's rows, all success = TRUE
SHOW TASKS IN DATABASE ALFA_EDW_PRD;
SELECT t."schema_name", t."name", t."state" FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) t
WHERE t."predecessors" = '[]'
  AND PARSE_JSON(t."task_relations"):"FinalizedRootTask" IS NULL   -- a finalizer has no predecessors either
ORDER BY 1, 2;                                     -- root tasks and their state
```

**What can go wrong:** no ticket or a self-approval: the job stops; drift gate `fail` with drift: reconcile (R5) or
re-run with `ACK_DRIFT = true` after the approver accepts it in the ticket; a migration fails part-way: **do not
re-run blindly** (Snowflake committed part of it), follow RUNBOOKS R1; the cross-environment guard fails the deploy
after the migrations committed (RUNBOOKS R10) or the task restore cannot bring a graph back (FAILED row, RUNBOOKS R9);
exit code 35: RUNBOOKS R7; the lock is held by another run: the job waits (RUNBOOKS R6 if it is stuck).

### F5. Hotfix path

**Who:** on-call DBA, release manager, a PRD approver, the emergency change approver. **Where:** Bitbucket, Jenkins,
and (cherry-pick only) a break-glass session host (B7 step 4).

A hotfix is a normal release on a short clock. Cherry-pick only when `main` holds unreleased work that must not reach
PRD yet. Details and recovery: RUNBOOKS R4.

**Do:**

1. Emergency change ticket.
2. Branch from `main`, scaffold a new migration (`fw.py new`; its fresh timestamp makes it newer than anything
   unreleased) or edit the repeatable; PR with expedited review; merge. Build N bundles it and deploys DEV.
3. **Fast path (preferred):** deploy N to one test runway, MGM and PRD back to back (F2-F4). Every gate still runs.
4. **Cherry-pick path:** the deploy jobs ship whole bundles and have no cherry-pick switch, so this is a break-glass
   run (B7: ticket, reserved locks `ALFA_EDW_MGM` / `ALFA_EDW_PRD`, service keys from the vault, a host inside the
   Jenkins egress range) from bundle N, downloaded from build N's archived artifacts:

   ```bash
   python3 scripts/fw.py verify-bundle --bundle edw-flyway-bundle-N.tar.gz --extract hotfix-N --require-branch main
   cd hotfix-N/bundle
   SHA=$(python3 -c 'import json; print(json.load(open("bundle-manifest.json"))["git_sha"])')
   eval "$(python3 scripts/fw.py env --runway MGM --format shell)"   # + the B7 exports
   flyway -environment=MGM info                     # the hotfix version is Pending
   flyway -environment=MGM migrate -cherryPick="2026.10.20.1405" \
     -environments.MGM.flyway.placeholders.deploy_id="MGM-hotfix-CHG-nnnn" \
     -environments.MGM.flyway.placeholders.git_sha="$SHA" \
     -environments.MGM.flyway.placeholders.build_url="<URL of main build N>" \
     -environments.MGM.flyway.placeholders.approver="<approver>"
   ```

   Then the same for PRD with `PRD` in place of `MGM`. Add a changed repeatable to `-cherryPick` by its description,
   exactly as `bundle-manifest.json` lists it (`repeatable[].description`). Before each target, rehearse the way the
   job does: run the SQL printed by `fw.py rehearsal-sql --runway PRD --action create` as the deploy role, run the
   same `migrate` with `-environment=RH_PRD` (and `RH_PRD` in the placeholder flags; `fw.py env --runway PRD` already
   exported `FLYWAY_RH_DB=ALFA_EDW_RH_PRD`), then run the SQL from `--action drop`. The skipped migrations stay
   pending and the next normal release applies them out of order (`outOfOrder = true` on every runway).
5. **Back-merge:** the fix is already on `main` because it went through step 2. If the incident was first fixed by
   hand in PRD, the back-merge is writing that same change as a migration on `main` the same day and reconciling the
   drift (RUNBOOKS R5).
6. Delete the key copies and work folders, unreserve the locks, record everything in the ticket.

**Verify:** on MGM and PRD the hotfix version is Success and the unreleased versions are still Pending; DEPLOY_LOG
has the hotfix `DEPLOY_ID`.

**What can go wrong:** the hotfix depends on an unreleased migration (the cherry-picked run fails: RUNBOOKS R1);
a changed repeatable left out of `-cherryPick`; running without reserved locks while a deploy job starts.

### F6. Rollback options

**Who:** on-call DBA with the release manager. **Where:** see RUNBOOKS R3 for the procedures.

| Situation                                    | Use               | How                                            |
|----------------------------------------------|-------------------|------------------------------------------------|
| A versioned migration failed part-way        | fix forward       | R1: inspect, clean up, `flyway repair`, new V  |
| A completed versioned change must go         | undo (`U` script) | R3; Enterprise; schema only, never data        |
| A view, function, procedure or task is wrong | previous revision | revert the R file on main; F5 fast path        |
| Data was damaged                             | Time Travel       | R3 + `bootstrap/40_point_in_time_recovery.sql` |
| Several of the above                         | combine           | newest change first; never `flyway clean`      |

Undo cannot reverse a partially failed migration and cannot restore data; repeatables cannot be undone at all, only
re-applied from an older revision. The deploy jobs have no undo action: on a test runway the lease lead runs
`flyway undo` by hand (B7); on MGM/PRD it is a break-glass run. Fix-forward is the default (A1, decision 8).

**Time Travel recovery** (`bootstrap/40_point_in_time_recovery.sql`, RUNBOOKS R3) runs as the runway's deploy role;
on MGM/PRD that is a break-glass session (B7) with the lock `ALFA_EDW_<ID>` reserved. A top-to-bottom run changes
nothing: every destructive statement is commented out, and the blocks that stop or restart schedules (Q0, Q9) or
re-grant (B4) refuse to run unless `confirm_template = '<A|B>:<deploy_id>'`. In PART 0 set `runway` and `deploy_id`
(the deploy to undo: it must start with `<runway>-`, or be `manual`); PART 2 finds the point (one second before
that deploy's STARTED row) and PART 3 compares row counts. Then run one template, statement by statement:

* one table: Q0 -> A1 (clone at the point) -> A2 (compare) -> A3 (swap) -> Q9;
* a whole schema (never `FLYWAY`): Q0 -> B1 (clone) -> B2 (compare) -> B2b (what a schema clone lacks) -> B3 (swap)
  -> B4 (the schema's own grants) -> Q9.

Q0 records the schema's writers (started root tasks and alerts, running dynamic tables, loading pipes) in
`FLYWAY.DEPLOY_TASK_STATE` as `RESTORE-<deploy_id>` and stops them; Q9 restarts exactly those. **A swap discards
every write made after the recovery point**, legitimate ETL rows included: they leave with the `_RESTORE_` evidence
copy, and the data owner decides which of them are re-inserted (A2 lists them) or whether the ETL re-runs for that
window. A dropped or replaced table is template C (UNDROP); a replaced view, function or procedure is template D
(the previous version from git).

---

## Part G - Enterprise preview checks

Flyway Enterprise's snapshot, `check -drift` and `check -changes` use a comparison engine that is a **preview** for
Snowflake, and the local Flyway reference docs still list comparison support only for SQL Server, Oracle, PostgreSQL
and MySQL. The kit therefore ships with its own drift gate (`fw.py drift-sql` / `drift-eval`, INFORMATION_SCHEMA based)
and keeps the Enterprise checks switched off (`save_snapshot = false`, `preview_checks = false`) until a spike proves
them.

### G1. Spike: prove snapshot and check -drift on this EDW

**Who:** an edw-dba member with the DEV service login. **Where:** a host inside the Jenkins egress range (B7 step
4), DEV.

**Do:**

1. Snapshot DEV into its snapshot history. The project's `schemas` lists only `FLYWAY`, so pass the EDW schemas
   (part of what the spike decides is whether that list must become permanent):

   ```bash
   flyway -environment=DEV -schemas="FLYWAY,<SCHEMA1>,<SCHEMA2>,..." snapshot -source=DEV \
     -filename="snapshotHistory:spike1"
   ```

2. Make deliberate out-of-band changes in DEV, one per object type you deploy: add a column to a scratch table,
   change a view, a procedure, a task, a stream, a pipe, a dynamic table.
3. Check for drift against that snapshot and keep the report:

   ```bash
   flyway -environment=DEV -schemas="FLYWAY,<SCHEMA1>,<SCHEMA2>,..." check -drift \
     -check.deployedSnapshot="snapshotHistory:spike1" -check.failOnDrift=true -reportFilename=reports/spike/drift.html
   ```

4. Undo the probe changes. Note runtime, the snapshot history table's name and size
   (`SHOW TABLES IN SCHEMA ALFA_EDW_DEV.FLYWAY;`), and any errors.
5. `check -changes` needs a dedicated build database that Flyway may clean, and `flyway clean` on Snowflake leaves
   tasks, streams and pipes behind; the kit does not configure one. Skip it unless the spike adds one.

**Pass criteria:** every probe change appears in the report and the command exits non-zero; nothing else is
reported (no false positives from placeholder-substituted names); runtime fits the deploy window for 50+ schemas;
the result is written down with the Flyway version used. Every command above is preview behaviour: verify on first
run, and repeat the spike after each Flyway upgrade.

**What can go wrong:** the engine refuses Snowflake or errors out (stop here; the kit's own drift gate stays the
only one); the snapshot is too large; some object types are not covered.

### G2. Switch the preview checks on per runway

**Who:** platform engineer (PR), release manager (agrees). **Where:** `runways.toml`.

**Do:** if G1 passed, start with MGM, then PRD (DEV optional):

1. `save_snapshot = true`: every deploy saves a snapshot after migrating (`-migrate.saveSnapshot=true`).
2. After one deploy with a snapshot, `preview_checks = true`: the deploy job runs `flyway check -drift` before the
   migrate and archives the report. It stays **advisory**; the blocking gate remains `drift_gate`.

If the spike needed the schema list, the platform team adds it to the runway environments in `flyway.toml` in the same
PR and re-runs guard.

**Verify:** the next deploys archive the drift report; the first one after enabling warns that no snapshot exists
yet (expected).

**What can go wrong:** preview regressions after an upgrade: set the flags back to `false` (one-line PR).

### G3. Drift gate from warn to fail

**Who:** release manager with the lead DBA. **Where:** `runways.toml`.

**Do:** after two consecutive clean releases on a runway (no unexplained drift), set its `drift_gate = "fail"`,
MGM first, then PRD. Tune `[drift]` (`ignore_schemas`, `ignore_name_patterns`, `allowed_ddl_users`) first, each change
by PR. From then on, drift stops the deploy until it is reconciled (RUNBOOKS R5) or an approver accepts it with
`ACK_DRIFT = true`, noted in the ticket.

Limit: the kit's drift query sees tables and views (`LAST_DDL`) and functions and procedures (`LAST_ALTERED`); tasks,
streams, pipes and alerts are covered only by the Enterprise check of G2. INFORMATION_SCHEMA shows only what the
querying role can see, so the job runs the drift query with the target's deploy login, never the auditor. To look at
drift by hand, use the runway's service login in a B7 step 4 session (break-glass for MGM/PRD) and the `sfsql`
helper from RUNBOOKS R0 (`snow sql` with the same key-pair login):

```bash
python3 scripts/fw.py drift-sql --runway MGM > drift.sql
sfsql --format json --filename drift.sql > rows.json
python3 scripts/fw.py drift-eval --runway MGM --rows rows.json      # exit 1 = the runway's gate would stop
```

**Verify:** a test change by hand in MGM (then reverted) stops a `DRY_RUN = true` deploy at the drift stage.

---

## Part H - Checklists

### H1. First deploy to a runway

- [ ] B2: `DESC USER SVC_EDW_FLYWAY_<ID>` fingerprint matches the key stored in `sf-flyway-<ID>`.
- [ ] B2: `EXECUTE TASK` granted; on MGM and PRD `EXECUTE ALERT` too (bootstrap/10 PART 2c grants it), elsewhere
      only if the runway resumes alerts; bootstrap/10 PART 5 shows no FAIL line.
- [ ] B3: `FLYWAY` schema with `DEPLOY_LOG` and `DEPLOY_TASK_STATE`, owned by `EDW_<ID>_DEPLOYER`; bootstrap/20
      PART 6 "kit table columns" PASS (`ROOT_FQN` present; after a kit upgrade, PART 0 + 2 + 6 re-run).
- [ ] B3: the access roles `EDW_<ID>_<SCHEMA>_READ` (and `_WRITE`) exist for every schema with a grants file
      (bootstrap/10 PART 2e) and have USAGE on the database (bootstrap/20 PART 1).
- [ ] D2: every schema owned by `EDW_<ID>_DEPLOYER` and managed access.
- [ ] A2/C2: the deploy job runs on a deploy agent (console: node name), and that node takes no `EDW/ci` builds.
- [ ] D5/D6: Baseline row present; auditor can `SELECT` the history table.
- [ ] Jenkins: `/EDW/deploy/<ID>` holds `sf-flyway-<ID>`, `sf-flyway-AUDIT`, `flyway-license`,
      `bitbucket-status-token`; the lock `ALFA_EDW_<ID>` exists.
- [ ] `python3 scripts/fw.py env --runway <ID>` shows the expected policy.
- [ ] A `DRY_RUN = true` deploy is green (for MGM/PRD: rehearsal clone created and dropped).
- [ ] First real deploy: DEPLOY_LOG STARTED and SUCCEEDED rows carry `DEPLOY_ID = '<ID>-<build>'`, not `manual`
      (this proves the deploy metadata placeholders reach Flyway).
- [ ] `deploy-<ID>` status visible on the commit in Bitbucket.
- [ ] Task graphs (roots and members) in the expected state; drift output clean or explained.

### H2. Release (MGM, then PRD)

- [ ] Candidate build N identified; green on SAN, SEA or PHX; `bundle-manifest.json` reviewed.
- [ ] Release notes: migrations, out-of-order items, waivers, long-running data fixes, smoke tests, rollback per change.
- [ ] MGM: `DRY_RUN` reviewed, deployed, approved by edw-release-approvers, smoke tests green.
- [ ] PRD: change ticket approved; window and on-call DBA booked; Time Travel retention checked.
- [ ] PRD: dry-run report and rehearsal reviewed; drift clean or `ACK_DRIFT` justified in the ticket.
- [ ] PRD: approver is not the starter; post-checks done; Time Travel point written into the ticket.
- [ ] Hotfix only: back-merge done (F5 step 5); break-glass key copies deleted; locks unreserved.

### H3. Quarterly

- [ ] **Key rotation** for every `SVC_EDW_FLYWAY_*` user, including AUDIT (RUNBOOKS R11): generate a new pair (B2),
      `ALTER USER <user> SET RSA_PUBLIC_KEY_2 = '<new key>';`, update the Jenkins credential (every copy for AUDIT),
      replace the user's vault entry with the new `.p8` and passphrase (access list as in B7), prove the key with a
      `DRY_RUN = true` deploy and with an R0 `flyway info` using the key checked out of the vault, then
      `ALTER USER <user> UNSET RSA_PUBLIC_KEY;` and destroy the old key's copies, old vault version included. Next
      quarter rotate the other slot.
- [ ] **Licence:** `flyway version -outputType=json` shows Enterprise and a `permitExpiry` more than 90 days away;
      CI PAT valid; offline permit renewed if used.
- [ ] **Bitbucket tokens:** the Jenkins repository access token and any webhook secret still valid; rotate per policy.
- [ ] **Access review:** `SHOW GRANTS OF ROLE EDW_<ID>_DEPLOYER;` lists only SYSADMIN and the service user on all
      six runways; vault access to the six service keys matches B7; SYSADMIN holders reviewed; AD approver groups,
      Jenkins role assignments and the Bitbucket break-glass group reviewed.
- [ ] **Leftovers:** `SHOW DATABASES LIKE 'ALFA_EDW_RH_%';` returns nothing outside a running deploy (RUNBOOKS R12).
- [ ] **Task budget:** `SHOW TASKS IN ACCOUNT;` then
      `SELECT COUNT(*) FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "state" = 'started';` well under 10,000.
- [ ] **Snowflake behaviour changes:** `SELECT SYSTEM$SHOW_ACTIVE_BEHAVIOR_CHANGE_BUNDLES();` (2026_08 makes
      COPY GRANTS the default for CREATE OR REPLACE; the kit already writes it explicitly).
- [ ] **Flyway upgrade review:** read the release notes; upgrade by one PR that changes `flyway_version` and
      `flyway_image` together, then a DEV deploy, a MGM `DRY_RUN`, and the G1 spike again if preview checks are on.
- [ ] **Waivers and drift allow-lists:** `grep -rn FLYWAY-WAIVER migrations repeatable` and `runways.toml [drift]`
      still justified.
