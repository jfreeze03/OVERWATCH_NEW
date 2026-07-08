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
- test_hardening_v21 / test_v22_features / test_v24_features /
  test_v25_features — regression locks for the 4.1—4.5 passes
- test_stress — opt-in (OW_STRESS=1 / `make stress`) render+logic volume

history_locks/ — frozen locks from earlier feature waves (V012—V018 era,
P1/P2 polish rounds). They still run in CI; they just don't need to crowd
the top level. Fix them only if a deliberate contract change breaks one.
