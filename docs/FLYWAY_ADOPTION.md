# Flyway adoption runbook

Written 2026-07-10, ahead of procurement — so the day Flyway lands it is a
config change, not a project. The repo has used Flyway's naming convention
(`V###__description.sql`, append-only, never edit an applied file) since
V001, so adoption is transport-only: Flyway replaces the paste-in-Snowsight
step, nothing about the migrations themselves changes.

## One-time setup

1. **Service context** — create a dedicated user + role (suggested:
   `FLYWAY_SVC` / `OVERWATCH_DEPLOY`) with OWNERSHIP-equivalent rights on
   `DBA_MAINT_DB.OVERWATCH`. Do NOT run Flyway as ACCOUNTADMIN — it would
   trip the break-glass panels, and it should not need to. Key-pair auth
   via the JDBC driver (Snowflake JDBC >= 3.11; assign the public key with
   `ALTER USER FLYWAY_SVC SET RSA_PUBLIC_KEY = ...`).
2. **Add the service user to `DEPLOY_ACTORS`** (Admin -> Settings, V033) so
   its warehouse changes read as MANAGED, not MANUAL.
3. **Config** — copy `snowflake/flyway.toml.example` next to your Flyway
   install and fill the account/key paths. Migrations location is
   `filesystem:snowflake/migrations`.
4. **Baseline at the applied tip.** Existing environments already carry the
   chain without a `flyway_schema_history` table:

       flyway baseline -baselineVersion=<current tip, e.g. 033>

   Flyway then tracks from the NEXT migration. Fresh/dev environments skip
   the baseline and replay from V001 — which is also the long-missing
   "spin up a dev copy" story.
5. **Validate compatibility once** before trusting it: replay all
   migrations into a scratch database (`flyway migrate` against a clone
   target). The `$$`-delimited scripting blocks are supported by the
   Snowflake plugin, but prove it on this account's files before the first
   production run.

## What changes / what stays

- **Changes**: `flyway migrate` applies pending migrations in order and
  refuses checksum-tampered files. The manual Snowsight ordering step —
  the most error-prone part of every deploy — goes away.
- **Stays**: the in-file `SCHEMA_VERSION` guards (defense against Snowsight
  bypass), the version-row MERGEs (the app reads SCHEMA_VERSION), the
  sqlglot parse gate, tests, and the fix-forward/append-only discipline
  (which is also Flyway's checksum rule — the conventions were chosen to
  match).
- **Admin -> Migrations** already reads `flyway_schema_history` when it
  exists (v4.16.0) and falls back to SCHEMA_VERSION until then. A
  `success = FALSE` row is the signal for the future OPS_MIGRATION_FAILED
  rule (ships with its scan arm when Flyway lands — no decorative config).

## Later, optional

- Convert `roles.sql` to a repeatable migration (`R__roles.sql`) so grant
  changes re-apply automatically when the file changes.
- Run `validate.sql` as an `afterMigrate` callback.
- CI: `flyway migrate` on merge to main — deploys become a merge, and
  `installed_on` timestamps become DEPLOY members on the incident timeline
  (design: docs/design/V029_INCIDENT_OBJECT.md).
