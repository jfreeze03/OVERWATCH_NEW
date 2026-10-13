# ALFA EDW Flyway kit

A copy-into-your-repository kit that deploys the ALFA EDW (Snowflake) with **Flyway Enterprise**, **Jenkins** and
**Bitbucket Cloud**. One immutable bundle per `main` build is promoted through six airport-coded databases
("runways"); every step is gated: static guard rules, Flyway code review, promotion checks, a per-database lock,
drift detection, a rehearsal on a zero-copy clone, and approvals.

Nothing here holds a secret: logins come from Jenkins folder credentials through Flyway's `${env.*}` resolvers.

## Runway map

```
 feature branch --PR--> main --(Jenkins)--> bundle: one immutable, fingerprinted archive per main build
                                              |
      automatic              leased test runways             preproduction             production
  +--------------+    +------------------------------+    +----------------+    +----------------+
  | DEV          |--->| SAN        SEA        PHX    |--->| MGM            |--->| PRD            |
  | ALFA_EDW_DEV |    | ALFA_EDW_SAN / _SEA / _PHX   |    | ALFA_EDW_MGM   |    | ALFA_EDW_PRD   |
  +--------------+    +------------------------------+    +----------------+    +----------------+
   main + branch       main + branch bundles,              main bundles only,     main bundles only,
   bundles; deploys    applied on DEV first;               applied on any one     applied on MGM first;
   on every merge      one project per lease               of SAN/SEA/PHX first;  rehearsal + four-eyes
                                                           rehearsal + approval   approval + change ticket
```

| Runway | Database | Tier | Bundles | Must already run on | Approval (Jenkins input) | Rehearsal clone |
|---|---|---|---|---|---|---|
| DEV | `ALFA_EDW_DEV` | nonprod | main, branch | - | - | - |
| SAN | `ALFA_EDW_SAN` | nonprod | main, branch | DEV | - | - |
| SEA | `ALFA_EDW_SEA` | nonprod | main, branch | DEV | - | - |
| PHX | `ALFA_EDW_PHX` | nonprod | main, branch | DEV | - | - |
| MGM | `ALFA_EDW_MGM` | prod | main | SAN or SEA or PHX | `edw-release-approvers` | `ALFA_EDW_RH_MGM` |
| PRD | `ALFA_EDW_PRD` | prod | main | MGM | `edw-prod-approvers`, four-eyes | `ALFA_EDW_RH_PRD` |

`runways.toml` is the source of this table (the pipeline reads it through `fw.py env`); `flyway.toml` holds the
matching Flyway environments `DEV`, `SAN`, `SEA`, `PHX`, `MGM`, `PRD` and the rehearsal environments `RH_MGM`, `RH_PRD`.
Every runway keeps its Flyway history in `<DB>.FLYWAY."flyway_schema_history"`.

## What is in the kit

```
flyway.toml           Flyway project: locations, safety settings, one environment per runway (no secrets)
runways.toml          pipeline policy per runway: lanes, predecessors, approvals, rehearsal, drift gate, credentials
sqlfluff.cfg          SQLFluff + Redgate RG rules for `flyway check -code`
rules/                Enterprise regex rules ALFA01-ALFA06 (no ALFA_EDW_ names, no USE, no CREATE OR REPLACE TABLE, ...)
migrations/<SCHEMA>/  versioned V<yyyy>.<mm>.<dd>.<hhmm>__<schema>_<desc>.sql (+ optional U undo, + .sql.conf)
repeatable/<SCHEMA>/  repeatable R__<layer>_<SCHEMA>__<object>.sql (views, functions, procedures, tasks, grants, ...)
callbacks/            deploy log, cross-environment reference guard, member-level task-graph snapshot and restore
scripts/fw.py         the kit CLI (Python 3.11+, standard library only): guard, new, bundle, env, promotion, drift
bootstrap/            Snowsight worksheets a Snowflake admin runs: accounts, runway databases, refresh, recovery
jenkins/              multibranch CI (Jenkinsfile), deploy job (Jenkinsfile.deploy), Job DSL seed, JCasC snippet
bitbucket/            branch restrictions (JSON + apply script) and CODEOWNERS
docs/                 GUIDE, CONVENTIONS, SHORTCOMINGS, RUNBOOKS
```

The SALES and FINANCE migrations and repeatables are worked examples that pass every rule; read them next to
`docs/CONVENTIONS.md`.

## Developer quick start (5 minutes)

You need git, Python 3.11+ and push access to the repository. Run everything from the repository root.

1. Branch from an up-to-date `main`:
   ```
   git fetch origin
   git switch -c feature/EDW-1234-order-status origin/main
   ```
2. Scaffold the file; never hand-type a migration name (the version is the current UTC time). Run ONE of these per
   change (the second is the first plus its U undo script; running both would create two migrations):
   ```
   python3 scripts/fw.py new --schema SALES --desc add_order_status            # versioned migration, or
   python3 scripts/fw.py new --schema SALES --desc add_order_status --undo     # versioned + U undo pair
   ```
   and, for a view, function, procedure, task or grants file:
   ```
   python3 scripts/fw.py new --schema SALES --repeatable --layer 30 --object v_open_orders
   ```
   It prints the new path, e.g. `migrations/SALES/V2026.10.13.1415__sales_add_order_status.sql`, with the house
   header and a template for the object type. Replace every `<...>` marker; the guard fails until you do (FG021).
3. Write the change following `docs/CONVENTIONS.md`: `SCHEMA.OBJECT` everywhere, never a database name,
   `COPY GRANTS` on every `CREATE OR REPLACE`, Snowflake Scripting and alerts inside `EXECUTE IMMEDIATE $$ ... $$`,
   new schemas `WITH MANAGED ACCESS`.
4. Run the guard against `main` (it also checks that no already-merged migration was edited):
   ```
   python3 scripts/fw.py guard --base origin/main
   ```
   Findings print as `path:line: FGnnn severity: message`; exit code 0 means no errors. When a finding is
   intended (for example a `DROP TABLE` in an undo script), add a waiver line to that file with a ticket:
   `-- FLYWAY-WAIVER: FG006 EDW-1234 undo of the create in the same version`.
5. Commit, push and open a pull request into `main`. Jenkins (`EDW/ci/edw-flyway`) runs the guard, checks the
   Flyway Enterprise licence, runs `flyway check -code` (SQLFluff + `rules/`), then `flyway info` and `validate`
   against DEV with the read-only auditor login, and reports the build to Bitbucket. Merging needs that green
   build and two approvals, one of them from a default reviewer.
6. After the merge, the `main` build bundles the repository and starts `/EDW/deploy/DEV/deploy`. Promotion to
   SAN / SEA / PHX, MGM and PRD is a deploy job per runway: `docs/GUIDE.md`, Part F.

`python3 scripts/fw.py --help` lists every command:

| Command | What it does (who runs it) |
|---|---|
| `guard [--base <ref>] [--junit f] [--json f] [--strict]` | rules FG001-FG021 (developers, every build) |
| `new --schema S --desc d [--undo] [--at ISO]` | scaffold a versioned migration (developers) |
| `new --schema S --repeatable --layer N --object o` | scaffold a repeatable (developers) |
| `bundle ...` / `verify-bundle ...` | build / re-hash the immutable deploy bundle (Jenkins) |
| `env --runway ID [--format shell\|properties\|json] [--auditor]` | a runway's settings from `runways.toml` |
| `promote-check --runway ID --info PRED=f.json --bundle-manifest m.json` | did the bundle run cleanly before? |
| `drift-sql --runway ID` / `drift-eval --runway ID --rows f.json [--ack]` | out-of-band DDL vs the drift gate |
| `rehearsal-sql --runway ID --action create\|drop` | SQL for the `ALFA_EDW_RH_<ID>` rehearsal clone |

## Where each rule is enforced

| Gate | Enforced by | When |
|---|---|---|
| Guard FG001-FG021 | `python3 scripts/fw.py guard` | locally, every PR build, every deploy |
| SQLFluff + Redgate RG rules, regex rules ALFA01-06 | `flyway check -code` (failOnError) | every PR build |
| Reviews, green build, no direct push to `main` | Bitbucket branch restrictions | every merge |
| Lane, promotion, lock, drift, rehearsal, approval | `jenkins/Jenkinsfile.deploy` + `runways.toml` | every deploy |
| Task graphs, deploy log, cross-environment refs | `callbacks/` | inside every `flyway migrate` |

The guard is the primary gate; `check -code` is the Enterprise second line. The guard is also the gate for every
waivable class: a code-review rule that repeats a waivable guard rule (ALFA03, ALFA06, RG06, RG13, RG08, RG11) is a
warning, so a file with a ticketed `-- FLYWAY-WAIVER` line passes code review and the report lists the statement.
Regex rules have no per-file waiver; the remaining SQLFluff errors (for example RG09, UPDATE without WHERE) are
overridden per line with `-- noqa: <code>`, which the code-review report lists (`docs/CONVENTIONS.md` section 8).
The Bitbucket merge checks (reviews, green build) block a merge only where the plan enforces them (believed to be
Premium; verify yours); without that, the deploy jobs' own gates still hold.

## Documentation

- [docs/GUIDE.md](docs/GUIDE.md): initial setup, accounts and access, Jenkins and Bitbucket setup, adopting the
  existing databases, and step-by-step deploys DEV -> SAN / SEA / PHX -> MGM -> PRD.
- [docs/CONVENTIONS.md](docs/CONVENTIONS.md): how to write each object type, placeholders, naming, review checklist.
- [docs/SHORTCOMINGS.md](docs/SHORTCOMINGS.md): Flyway's known shortcomings on Snowflake and the rule, script or
  process that mitigates each.
- [docs/RUNBOOKS.md](docs/RUNBOOKS.md): failed migration, rollback, hotfix, drift, licence, stuck lock, runway
  refresh, key rotation.

## Copy into the EDW repository: checklist

- [ ] Copy the contents of `flyway-edw/` to the **root** of the EDW repository (the Jenkins jobs expect
      `jenkins/Jenkinsfile` and `jenkins/Jenkinsfile.deploy` there). Leave out `__pycache__/`, `dist/`, `reports/`.
- [ ] Move `bitbucket/CODEOWNERS` to `.bitbucket/CODEOWNERS`; replace the `@alfa/...` workspace and group slugs.
- [ ] Delete the sample `migrations/SALES/`, `migrations/FINANCE/` and `repeatable/SALES/` files before the first
      real deploy: they would create real objects on every runway.
- [ ] `runways.toml`: set `kit.baseline_version` to the version you baseline with (GUIDE Part D); confirm the
      `flyway_version` / `flyway_image` / `snowflake_cli` pins and that the approval groups exist in Jenkins' security
      realm. Point `python_image` at an internal image with git and the Snowflake CLI, and pin both images by digest
      (GUIDE A2). `flyway.toml`: confirm the `etl_wh` and `role_prefix` placeholder values match real warehouse /
      role names.
- [ ] `python3 scripts/fw.py guard` reports 0 errors.
- [ ] Snowflake: run `bootstrap/10_account_objects.sql` (shared part, then per runway: deploy identity, EXECUTE TASK,
      on MGM / PRD also EXECUTE ALERT and CREATE DATABASE, and the access roles listed in `access_roles` that the
      grants files use) and `bootstrap/20_runway_database.sql` per runway (GUIDE Part B).
- [ ] Upgrading an earlier copy of the kit instead: `callbacks/` holds exactly the new kit's files (a renamed callback
      must not survive under its old name), and `bootstrap/20_runway_database.sql` PART 0 + 2 + 6 is re-run on every
      runway before the first deploy of the new kit; without the new `FLYWAY.DEPLOY_TASK_STATE.ROOT_FQN` column every
      deploy stops in `beforeMigrate__20` with error -20121 (GUIDE C1, B3).
- [ ] Jenkins, in this order (GUIDE Part C, then B4): set up dedicated deploy agents that only `EDW/deploy/*` jobs
      may use (GUIDE A2, C2); apply `jenkins/casc-snippet.yaml` (C2, it sets `BITBUCKET_WORKSPACE` / `BITBUCKET_REPO`
      that the seed needs); run the `jenkins/jobs.groovy` seed (C3, it creates the folders); then create the folder
      credentials named in `runways.toml` (B4).
- [ ] Adopt the databases per GUIDE Part D: D2 ownership transfer, D5 baseline PRD and DEV, D6 refresh MGM / SAN /
      SEA / PHX from PRD (they arrive baselined) or reconcile them in place.
- [ ] Bitbucket: add default reviewers (individual users), then run
      `bitbucket/apply_branch_restrictions.sh --dry-run` and apply it (GUIDE Part B).
- [ ] Open a first pull request, see it go green, merge, watch the DEV deploy; then follow GUIDE Part F.

The kit's own automated tests live in the OVERWATCH repository (`tests/test_flyway_edw_kit.py`) and are not copied.
