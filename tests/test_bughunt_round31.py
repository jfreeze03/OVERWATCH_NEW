"""Bug-hunt round 31: security correctness (escalation, coverage-gating, identity, change-risk).

6 confirmed (coverage-posture finder errored = known gap; 1 refuted as a scoring-calibration
judgment). 5 fixed here in app code; the 6th (a HIGH over-broad change-risk exclusion) is in the
V088 view -> owner-gated migration (authored separately).

#3 (MED): failed_login_reasons_fact SOURCE_IPS counted the loader's COALESCE(CLIENT_IP,'(none)')
   sentinel as a distinct source IP, inflating the spray signal by 1 vs the live twin -- the
   missed sibling of the r28b failed_logins_fact fix. NULLIF it.
#2 (MED): Trust Center live fallback painted "every scanner came back clean" on an EMPTY read with
   no freshness gate (the path taken only when the mart's freshness could NOT be confirmed). An
   empty FINDINGS read = nothing scanned, not nothing at risk -> needs_setup, not a green all-clear.
#6 (MED): the CHANGE RISK coverage gate keyed only on loader-run freshness (SP_LOAD_SECURITY_FACTS
   stamps OK/now every hour), so a stalled OW_QH_EXTRACT read as Healthy-100 with no fresh changes.
   Now also requires the EXTRACT to be fresh (distinguishes a stalled feed from a quiet account).
#4 (LOW): governance_counts MFA_GAP_USERS scored a clean 0 when FACT_LOGIN_DAILY had no trailing-30d
   coverage (C8 no-data-vs-clean). Returns NULL when uncovered; the panel drops NULL/NaN signals so
   they surface as unresolved.
#1 (LOW): effective_access LIMIT 3000 ORDER BY RISK_SCORE DESC could truncate a low-base-risk
   MANAGE-GRANTS-only path (score 25) out of a huge account, a self-escalation false-negative. The
   ORDER BY now floats any manage-bearing path to the top so the signal survives the cap.
"""

from __future__ import annotations

from pathlib import Path

from app.data import security_sql

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- #3: the reasons-fact spray signal drops the '(none)' sentinel, like the live twin ---

def test_failed_login_reasons_fact_excludes_none_sentinel_from_source_ips():
    sql = security_sql.failed_login_reasons_fact(30)
    assert "COUNT(DISTINCT NULLIF(CLIENT_IP, '(none)')) AS SOURCE_IPS" in sql
    assert "COUNT(DISTINCT CLIENT_IP) AS SOURCE_IPS" not in sql
    # the live twin (raw LOGIN_HISTORY, real NULLs) needs no sentinel handling
    assert "COUNT(DISTINCT CLIENT_IP)" in security_sql.failed_login_reasons(30)


# --- #1: a manage-bearing (self-escalation) path always survives the effective-access cap ---

def test_effective_access_protects_manage_paths_from_the_row_cap():
    sql = security_sql.effective_access("ALFA")
    assert "GREATEST(RISK_SCORE, IFF(COALESCE(p.MANAGE_GRANTS, 0) > 0, 100, 0))" in sql
    # the naive RISK_SCORE-only ordering (which could drop a score-25 manage-only path) is gone
    assert "ORDER BY RISK_SCORE DESC, r.USER_NAME" not in sql


# --- #2: an empty Trust Center read is not a verified all-clear on the freshness fallback ---

def test_trust_center_empty_read_is_not_a_false_all_clear():
    src = _read("app/ui/pages/security.py")
    body = src.split("def _trust_center_tab", 1)[1].split("\ndef ", 1)[0]
    assert 'empty_state("clean", "No findings — every scanner came back clean.")' not in body
    assert 'empty_state("needs_setup"' in body and "nothing was scanned" in body


# --- #6: CHANGE RISK coverage requires the change-feed EXTRACT to be fresh, not just the loader ---

def test_change_risk_coverage_gates_on_extract_freshness():
    sql = security_sql.security_domain_coverage()
    assert "OW_QH_EXTRACT" in sql and "QHX_FRESH" in sql
    # the CHANGE RISK branch's COMPLETE now also requires the extract to be fresh
    change = sql.split("'CHANGE RISK'", 1)[1].split("UNION ALL", 1)[0]
    assert "AND QHX_FRESH" in change
    # IDENTITY keeps its own NEWEST data-recency gate (unchanged)
    assert "NEWEST >= DATEADD('day', -2, CURRENT_DATE())" in sql


# --- #4: MFA gap reads UNKNOWN (not clean 0) when the login fact has no coverage --------

def test_governance_mfa_gap_is_null_without_login_coverage():
    sql = security_sql.governance_counts()
    assert ("IFF((SELECT COUNT(*) FROM DBA_MAINT_DB.OVERWATCH.FACT_LOGIN_DAILY\n"
            "         WHERE DAY >= DATEADD('day', -30, CURRENT_DATE())) = 0, NULL,") in sql
    # the panel drops NULL/NaN governance signals so they surface as unresolved, not a clean 0
    panel = _read("app/ui/pages/security.py").split("def _governance_score_panel", 1)[1].split("\ndef ", 1)[0]
    assert "isinstance(v, float) and pd.isna(v)" in panel
