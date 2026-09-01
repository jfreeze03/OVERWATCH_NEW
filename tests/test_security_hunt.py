"""Security-layer bug-hunt locks (2026-08-30).

App-side fixes shipped in v4.352.0 alongside the owner-gated V100 migration:
  H1  break-glass panel reads LIVE admin_role_activity (all statements), not the
      change-only FACT_SECURITY_CHANGE twin
  H2  security_exception_queue caps PER DOMAIN so a noisy arm can't starve another out of
      the scoring frame (false all-clear)
  H4  the export-pack MFA sheet proves an empty mart against live before writing an empty CSV
  M1  render_security_overview gates on queue.ok before scoring (no green on read failure)
  M2  admin_grant_context off-hours flag is account-local (Chicago), not the session clock
  M3  day_grants scopes GRANTS_TO_USERS by the GRANTEE, not the granted role name
  M4  the Access-tab MFA panel shows "unconfirmed" when the live fallback itself fails
  L1  failed_login_reasons uses the same coarse ERROR_CATEGORY buckets as its fact twin
  L2  the unused-roles export recommends REVIEW, not REVOKE (inheritance caveat)
"""

from __future__ import annotations

from pathlib import Path

from app.data import security_sql
from app.logic.least_privilege import _ACCESS_REVIEW_RECOMMEND

_ROOT = Path(__file__).resolve().parents[1]
_SEC = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
_CTR = (_ROOT / "app" / "ui" / "security_center.py").read_text(encoding="utf-8")


# --- H2: per-domain cap, not a cross-domain LIMIT --------------------------------------
def test_exception_queue_caps_per_domain_not_globally():
    sql = security_sql.security_exception_queue("ALFA", 100)
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY DOMAIN" in sql
    assert "LIMIT" not in sql.split("QUALIFY", 1)[1]          # no trailing global LIMIT


# --- M2: admin-grant off-hours is account-local (Chicago) ------------------------------
def test_admin_grant_context_uses_account_local_time():
    sql = security_sql.admin_grant_context(90, "ALFA")
    assert "HOUR(CONVERT_TIMEZONE('America/Chicago', g.CREATED_ON))" in sql
    assert "DAYOFWEEKISO(CONVERT_TIMEZONE('America/Chicago', g.CREATED_ON))" in sql
    assert "HOUR(g.CREATED_ON)" not in sql                    # raw session-tz form gone


# --- M3: day_grants scopes by the grantee, not the role name ---------------------------
def test_day_grants_scopes_by_grantee_not_role():
    sql = security_sql.day_grants("2026-07-07", "Trexis")
    assert "COMPANY_FOR_USER(GRANTEE_NAME)" in sql            # grantee-scoped
    assert "UPPER(ROLE) LIKE '%TRXS%'" not in sql             # not role-name scoped
    # ALL stays account-wide
    assert "COMPANY_FOR_USER" not in security_sql.day_grants("2026-07-07")


# --- L1: login-reason grain matches the fact twin --------------------------------------
def test_failed_login_reasons_uses_coarse_error_categories():
    live = security_sql.failed_login_reasons(30, "ALFA")
    # same buckets the fact loader (V075) writes, so the panel is path-invariant
    for bucket in ("'NETWORK POLICY'", "'DISABLED USER'", "'MFA'", "'CREDENTIAL'", "'OTHER'"):
        assert bucket in live, bucket
    assert "COALESCE(ERROR_MESSAGE, 'UNKNOWN') AS REASON" not in live   # raw-message grain gone


# --- H1: break-glass panel reads all statements live; the fact twin is documented ------
def test_breakglass_panel_reads_live_all_statements():
    tab = _SEC.split("Break-glass role activity", 1)[1].split("section_header", 1)[0]
    assert "security_sql.admin_role_activity(days, company, bounds=bounds)" in tab
    assert "admin_role_activity_fact" not in tab
    # the change-only fact builder now warns against reuse behind an all-statements panel
    assert "does NOT see SELECT/COPY/CALL" in (
        _ROOT / "app" / "data" / "security_sql.py").read_text(encoding="utf-8")


# --- H4: export-pack MFA sheet proves empty against live -------------------------------
def test_export_pack_mfa_sheet_has_live_proof():
    pack = _SEC.split("def _export_pack", 1)[1].split("\ndef ", 1)[0]
    assert 'name == "mfa_gaps_password_login" and res.ok and res.empty' in pack
    assert "users_without_mfa_live(company)" in pack


# --- M1: overview gates on queue.ok before scoring -------------------------------------
def test_overview_does_not_score_when_queue_unresolved():
    body = _CTR.split("def render_security_overview", 1)[1].split("\ndef ", 1)[0]
    # the queue-fail guard (returns) appears BEFORE domain_posture is called
    assert body.index("if not queue.ok:") < body.index("posture = domain_posture(")


# --- M4: Access-tab MFA panel is honest when the live fallback fails --------------------
def test_access_mfa_panel_flags_unproven_when_fallback_fails():
    body = _SEC.split("MFA gaps with password-login evidence", 1)[0]
    assert "_mfa_unproven = True" in body
    assert "if _mfa_unproven:" in _SEC


# --- L2: unused-roles export recommends REVIEW, not REVOKE ------------------------------
def test_unused_roles_recommend_is_review_not_revoke():
    rec = _ACCESS_REVIEW_RECOMMEND["unused_roles_90d"]
    assert rec.startswith("REVIEW")
    assert "via inheritance" in rec
    assert "REVOKE" not in rec
