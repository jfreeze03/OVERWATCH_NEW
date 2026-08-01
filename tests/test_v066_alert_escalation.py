"""Locks for V066 — bug round 6's 5 migration-proc fixes.

Three procs re-derived (SP_ALERT_SCAN + SP_LOAD_MARTS_V27 from V062, SP_ALERT_SCAN_DAILY
from V065) via count-asserted needle edits in outputs/gen_v066.py:

  #1  PIPE_COPY_FAILURES     -- daily dedupe key gains a CRIT/WARN severity band
  #6  COST_SERVERLESS_CREEP  -- scan window excludes today (equal 7-complete-day weeks)
  #11 COST_DEPT_BUDGET_PACE  -- daily dedupe key gains a HIGH/MED severity band
  #2  COST_CONTRACT_BREACH   -- weekly dedupe key gains a CRIT/WARN band (AI_CREEP left alone)
  #3  MART_INCIDENT_TIMELINE arm [8] -- DELETE+INSERT wrapped in one transaction
  #37 VALIDATE SCOPE         -- SP_LOAD_MARTS_V27 RAISEs on a scope outside HOURLY/DAILY
  #23 AI FRESHNESS PARTIAL   -- DAILY stamp needs BOTH FACT_AI_USAGE_DAILY arms loaded

The generator is the source of truth: the first test regenerates and byte-compares.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V66 = (_MIG / "V066__alert_escalation_serverless_window_timeline_atomicity.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    return re.search(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S).group(0)


def test_v066_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v066.py")],
                       env={**os.environ, "V066_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V66, (
        "V066 drifted from outputs/gen_v066.py — regenerate, never hand-edit.")


def test_v066_guard_and_version():
    assert "EXCEPTION (-20066" in _V66 and "IF (v < 65) THEN" in _V66
    assert "SELECT 66 AS VERSION" in _V66 and "WHERE VERSION = 66)" in _V66
    assert _V66.count("CREATE OR REPLACE PROCEDURE") == 3   # 3 procs re-derived
    assert "CREATE TABLE" not in _V66 and "CREATE TASK" not in _V66


# ---------------------------------------------------------------------------
# escalation dedupe bands (SP_ALERT_SCAN #1/#11, SP_ALERT_SCAN_DAILY #2)
# each band must MATCH its rule's own severity-escalation threshold
# ---------------------------------------------------------------------------
def test_pipe_copy_dedupe_band_matches_critical_threshold():
    scan = _proc(_V66, "SP_ALERT_SCAN")
    # SEVERITY escalates at FAILED_FILES >= 10; the dedupe band must flip on the same test
    assert "IFF(p.FAILED_FILES >= 10, 'CRITICAL', c.SEVERITY)" in scan     # severity (unchanged)
    assert "IFF(p.FAILED_FILES >= 10, 'CRIT', 'WARN') || '|' || TO_VARCHAR(CURRENT_DATE())" in scan
    assert "p.TBL || '|' || TO_VARCHAR(CURRENT_DATE())" not in scan        # bare key gone


def test_dept_pace_dedupe_band_matches_high_threshold():
    scan = _proc(_V66, "SP_ALERT_SCAN")
    assert "IFF(d.OVER_PCT >= c.THRESHOLD_NUM * 3, 'HIGH', c.SEVERITY)" in scan   # severity
    assert "IFF(d.OVER_PCT >= c.THRESHOLD_NUM * 3, 'HIGH', 'MED') || '|' || TO_VARCHAR(CURRENT_DATE())" in scan
    assert "d.DEPARTMENT || '|' || TO_VARCHAR(CURRENT_DATE())" not in scan        # bare key gone


def test_contract_breach_band_leaves_ai_creep_alone():
    daily = _proc(_V66, "SP_ALERT_SCAN_DAILY")
    assert "IFF(p.DAYS_LEFT <= 14, 'CRITICAL', c.SEVERITY)" in daily              # severity
    assert "IFF(p.DAYS_LEFT <= 14, 'CRIT', 'WARN') || '|' || TO_VARCHAR(DATE_TRUNC('week'" in daily
    # exactly one bare weekly key remains — COST_AI_CREEP, which has no within-bucket escalation
    assert daily.count("c.RULE_ID || '|' || TO_VARCHAR(DATE_TRUNC('week', CURRENT_DATE()))") == 1


# ---------------------------------------------------------------------------
# #6 serverless creep — today excluded (equal 7-complete-day weeks)
# ---------------------------------------------------------------------------
def test_serverless_creep_excludes_today():
    scan = _proc(_V66, "SP_ALERT_SCAN")
    assert ("WHERE USAGE_DATE >= DATEADD('day', -14, CURRENT_DATE())\n"
            "              AND USAGE_DATE < CURRENT_DATE()") in scan
    # the week boundaries are unchanged; today-exclusion does the work
    assert "SUM(IFF(USAGE_DATE >= DATEADD('day', -7, CURRENT_DATE()), CREDITS_USED, 0)) AS THIS_WK" in scan


# ---------------------------------------------------------------------------
# #3 incident-timeline arm [8] — atomic DELETE+INSERT
# ---------------------------------------------------------------------------
def test_incident_timeline_arm_is_atomic():
    marts = _proc(_V66, "SP_LOAD_MARTS_V27")
    arm = marts.split("[8] incident timeline", 1)[1].split("loaded := loaded || 'timeline ';", 1)[0]
    assert "BEGIN TRANSACTION;" in arm                                   # opens before the DELETE
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.MART_INCIDENT_TIMELINE" in arm
    assert marts.count("CURRENT_TIMESTAMP());\n            COMMIT;\n            loaded := loaded || 'timeline ';") == 1
    assert "ROLLBACK;   -- V066 #3" in marts                             # undo on a failed rebuild


# ---------------------------------------------------------------------------
# #10 FALSE SUCCESS — SP_LOAD_MARTS_V27 terminal verdict
# ---------------------------------------------------------------------------
def test_marts_verdict_replaces_false_success():
    marts = _proc(_V66, "SP_LOAD_MARTS_V27")
    assert "req_fail INT DEFAULT 0;" in marts and "opt_fail INT DEFAULT 0;" in marts
    # the unconditional 'marts loaded' claim is gone; a machine-readable verdict replaces it
    assert "V27 marts loaded (" not in marts
    assert "IF (req_fail = 0) THEN" in marts
    assert "RETURN 'MARTS OK (" in marts
    assert ("RETURN 'MARTS WITH ERRORS: ' || :req_fail || ' required, ' || :opt_fail "
            "|| ' optional ('") in marts


def test_marts_required_optional_failure_counters():
    marts = _proc(_V66, "SP_LOAD_MARTS_V27")
    # 13 arms swallow-and-count: 9 core facts/marts REQUIRED, 4 OPTIONAL
    # (tag-coverage, per-node timing, AI code + AI functions)
    assert marts.count("'mart_load_failed'") == 13          # every arm still swallows
    assert marts.count("req_fail := req_fail + 1;") == 9
    assert marts.count("opt_fail := opt_fail + 1;") == 4
    for ctx in ("MART_TAG_COVERAGE_DAILY - other marts unaffected",
                "MART_TASK_NODE_DAILY - other marts unaffected",
                "FACT_AI_USAGE_DAILY (code views) - other marts unaffected",
                "FACT_AI_USAGE_DAILY (functions view optional) - other marts unaffected"):
        seg = marts.split(ctx, 1)[1].split("END;", 1)[0]
        assert "opt_fail := opt_fail + 1;" in seg, ctx
    for ctx in ("MART_WAREHOUSE_EFFICIENCY_DAILY - other marts unaffected",
                "MART_COST_ALLOCATION_DAILY - other marts unaffected",
                "MART_INCIDENT_TIMELINE - other marts unaffected",
                "MART_SECURITY_POSTURE_DAILY - other marts unaffected"):
        seg = marts.split(ctx, 1)[1].split("END;", 1)[0]
        assert "req_fail := req_fail + 1;" in seg, ctx


# ---------------------------------------------------------------------------
# #11 FRESHNESS ADVANCES ON FAILURE — per-scope stamp gated on actually-loaded sources
# ---------------------------------------------------------------------------
def test_freshness_stamp_advances_only_for_loaded_sources():
    marts = _proc(_V66, "SP_LOAD_MARTS_V27")
    # both per-scope freshness MERGEs are token-gated off :loaded (the successful-arm set)
    assert marts.count("ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' '))") == 2
    # per-source STATUS from that source's own token(s), never the whole successful-arm list
    assert marts.count("LISTAGG(m.TOKEN, ' ') AS STATUS") == 2
    assert "STATUS = :loaded" not in marts
    # the static source-name IN-lists (which stamped even failed sources) are gone, both scopes
    assert "WHERE SOURCE_NAME IN ('MART_WAREHOUSE_EFFICIENCY_DAILY'" not in marts
    assert "WHERE SOURCE_NAME IN ('MART_SECURITY_POSTURE_DAILY'" not in marts
    # FACT_AI_USAGE_DAILY maps BOTH its arms so its collapsed row matches the MERGE target once
    assert "('FACT_AI_USAGE_DAILY', 'ai_code')" in marts
    assert "('FACT_AI_USAGE_DAILY', 'ai_functions')" in marts
    # the timeline source is still stamped — its token is appended after the #3 COMMIT
    assert "('MART_INCIDENT_TIMELINE', 'timeline')" in marts


# ---------------------------------------------------------------------------
# #37 VALIDATE SCOPE — an invalid scope RAISEs instead of a silent no-op load
# ---------------------------------------------------------------------------
def test_marts_scope_guard_fails_on_invalid_scope():
    marts = _proc(_V66, "SP_LOAD_MARTS_V27")
    # the guard exception is declared and RAISEd for any scope outside HOURLY/DAILY
    assert "bad_scope EXCEPTION (-20661," in marts
    assert ("IF (UPPER(:SCOPE) NOT IN ('HOURLY', 'DAILY')) THEN\n"
            "        RAISE bad_scope;\n"
            "    END IF;") in marts
    # the guard sits ABOVE both scope arms, so a typo can never reach a no-op load + success RETURN
    head = marts.split("IF (UPPER(:SCOPE) = 'HOURLY') THEN", 1)[0]
    assert "RAISE bad_scope;" in head
    # RAISE is not swallowed: no outer EXCEPTION handler wraps the scope arms (the outer
    # BEGIN's only END closes the proc), so the raise propagates out and the proc fails loudly
    assert marts.count("RAISE bad_scope;") == 1


# ---------------------------------------------------------------------------
# #23 AI FRESHNESS PARTIAL — FACT_AI_USAGE_DAILY stamped fresh only when BOTH arms loaded
# ---------------------------------------------------------------------------
def test_ai_freshness_requires_both_arms_loaded():
    marts = _proc(_V66, "SP_LOAD_MARTS_V27")
    # the DAILY stamp gates on ALL of a source's tokens, not any single one
    assert "HAVING COUNT(*) = COUNT_IF(ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' ')))" in marts
    # the per-token WHERE that stamped on a SINGLE arm is gone from DAILY; only the
    # single-token HOURLY MERGE keeps a WHERE ARRAY_CONTAINS
    assert marts.count("WHERE ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' '))") == 1
    # the all-arms HAVING gate is on the DAILY (multi-arm) MERGE only
    assert marts.count("HAVING COUNT(*) = COUNT_IF(ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' ')))") == 1
    # both AI arms are still mapped to the one physical FACT_AI_USAGE_DAILY source
    assert "('FACT_AI_USAGE_DAILY', 'ai_code')" in marts
    assert "('FACT_AI_USAGE_DAILY', 'ai_functions')" in marts
    # the guard lives in the DAILY freshness MERGE (posture + AI), not the HOURLY one
    daily_merge = marts.split("MART_SECURITY_POSTURE_DAILY', 'posture'", 1)[1]
    assert "HAVING COUNT(*) = COUNT_IF(ARRAY_CONTAINS(m.TOKEN::VARIANT, SPLIT(:loaded, ' ')))" in daily_merge


def test_v066_preserves_v065_run_rate_windows():
    daily = _proc(_V66, "SP_ALERT_SCAN_DAILY")
    # V065's forecast + AI-creep fixes ride through untouched
    assert "NULLIF(COUNT(DISTINCT CASE WHEN DAY < CURRENT_DATE() THEN DAY END), 0) AS DAILY_RATE_USD" in daily
    assert "AND DAY < CURRENT_DATE()   -- V065 rank3" in daily
    assert "NULLIF(COUNT(DISTINCT DAY), 0)" in daily        # V064 contract burn


def test_v066_in_migration_registry():
    # V066 is registered in the admin contract. (The validate.sql floor is the MOVING tip,
    # guarded generically by test_v451_trust — a per-migration test must not pin it.)
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 66 in _EXPECTED_MIGRATIONS
