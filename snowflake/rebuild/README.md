# One-shot full rebuild (generated 2026-07-12)

The runbook is docs/FULL_REBUILD.md; this folder is that runbook as six
paste-and-run files. Snowsight, deployment role (SNOW_ACCOUNTADMINS),
run 00 -> 05 in order, each with Run All, verifying the last result pane
before moving on:

| # | file | what | verify |
|---|------|------|--------|
| 00 | 00_backup_operator_data.sql | date-stamped clones of all 21 operator tables | SOURCE_ROWS == CLONE_ROWS every row |
| 01 | 01_teardown_rebuildables.sql | drops every rebuildable OVERWATCH object (operator data survives) | VERIFY select lists ONLY operator tables |
| 02 | 02_migrations_V001_V042.sql | all 42 migrations, in order | runs to the end; halts AT the failure if any |
| 03 | 03_roles.sql | grants incl. the V041 objects | 'roles applied' |
| 04 | 04_backfill_365.sql | year of dailies, 90d marts (extract first) | backfill coverage select |
| 05 | 05_validate.sql | post-install checks | every row OK |

Then, one hour later: snowflake/loader_chain_check.sql — every task
'started', freshness < 2h behind. Fleet board after 24h.

These files are GENERATED and equality-locked against their sources
(tests/test_rebuild_bundle.py) — edit the sources, never this folder.
Factory reset instead (drop operator data too): see docs/FULL_REBUILD.md
step 0; it is deliberately NOT part of this bundle.
