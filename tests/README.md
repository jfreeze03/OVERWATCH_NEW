# Test suite map

Living gates (run + evolve with every change):
- test_pages_apptest / test_operator_gating — AppTest page smokes + profile nav
- test_navigation_consistency — router targets proven against page source
- test_teardown_coverage — every created object covered by teardown.sql
- test_p0_polish — UX contracts (no migration-speak outside Admin, ...)
- test_sql_builders / test_injection_fuzz / test_sqlsafe / test_companies —
  SQL shape, scoping, and the strip-literals injection invariant
- test_formula_audit — hand-verified math expectations (fact-check 2026-07)
- test_formulas / test_anomaly / test_forecast / test_scoring / test_sizing /
  test_actions / test_insights / test_cortex / test_chargeback /
  test_change_impact / test_ai / test_status_colors / test_design_system /
  test_user_prefs / test_teardown_coverage — domain units
- test_hardening_v21 — regression lock for the 4.1 pass (the 4.2—4.5 locks
  live in history_locks/)
- test_stress — opt-in (OW_STRESS=1 / `make stress`) render+logic volume;
  test_stress_harness_targets keeps its stub targets honest without OW_STRESS
- test_usage_sim — CI run of usage_sim.py, the headless queries-per-interaction
  profiler (`python tests/usage_sim.py` for the text report)
- conftest.py — session-scoped autouse guard: no test ever opens a real
  Snowflake session (refuses app.core.session._connect; SNOWFLAKE_HOME points
  at an empty temp folder); it also back-ports the streamlit 1.55 AppTest
  ButtonGroup fix onto the 1.52.2 floor so the shaped AppTests run on both CI
  legs (locked by test_floor_apptest_shim; delete it once the floor reaches 1.55)

Phase 4 — locks that DERIVE their targets instead of listing them, so a builder
or module added tomorrow is covered without anyone remembering to add it:
- test_p4_filter_matrix — introspects app/data/*_sql.py and drives every
  company-taking builder (105, vs the 28 test_injection_fuzz names by hand)
  across all of COMPANIES: parses as Snowflake, filter is live, scopes stay
  distinct, hostile input stays inside a literal, day windows stay clamped
- test_p4_dst — account_today()/account_now() across the DST transitions, plus
  an AST guard that no module in app/logic or app/data reads the server clock
  for a business date (they must go through account time — the marts store it)
- test_p4_org_reconciliation — billed (ORGANIZATION_USAGE.USAGE_IN_CURRENCY)
  vs computed (credits x rate); pins the cloud-services rebate that
  formulas.billed_credits exists to make unforgettable
- test_release_lockstep — derives the migration tip (both validate.sql
  literals + the rebuild copy, the DEPLOYMENT/README run lists, the bundle
  name, V001..VNNN contiguity) and APP_VERSION (= the dated top CHANGELOG
  heading) from the repo, and FAILS if any test pins the tip or APP_VERSION
  again — a new migration or release edits no per-migration test
- tests/_source.py — the shared source reader (not collected): `read`,
  `page_source` (a page shell + the parts package it owns), `migration_tip`,
  `changelog_entry` (one CHANGELOG section by heading). Import it as
  `from tests._source import ...`; new tests use it instead of a local `_src`
- tests/_access.py — the shared admin-role roster script (not collected, v4.611.0):
  `dsa_rows`, `script_dsa_roster` (an identified SiS viewer whose
  SNOW_PRI_GFR_PRD_ALFA_DSA lookup answers a scripted member list). Roles alone
  decide who is an admin, so a test makes an admin only through the roster,
  never through a username; test_roles_only_access locks that

migrations/ — per-migration lock modules (test_v027_* … test_v165_*): each
locks its own migration's SQL contract (the newer ones its run-doc line too)
and never pins the tip (house law 5); test_generators_regenerate re-runs every
outputs/gen_v*.py and checks it reproduces the checked-in migration (house
law 1).

history_locks/ — frozen locks from earlier feature waves (V012—V018 era,
P1/P2 polish rounds, the 4.2—4.5 test_v22/v24/v25_features passes). They still
run in CI; they just don't need to crowd the top level. Fix them only if a
deliberate contract change breaks one.
