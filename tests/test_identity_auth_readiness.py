"""Next-Fifty #9 (v4.589.0): identity auth readiness for Snowflake's single-factor password deprecation
(rolling enforcement Aug–Oct 2026). A pure classifier (app/logic/identity_auth.py) grades every enabled
password holder / LEGACY_SERVICE user / admin WILL BREAK / MIGRATE / READY and emits review-then-run
ALTER USER stubs; the Security page adds the panel, an 'Admins with password and no MFA' KPI (regardless
of recent login evidence), probe-gated admin network-policy coverage, and a service-account band on the
dormant/reawakening scans. Every new read degrades to needs_setup, never a false red/green."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.data import security_sql
from app.logic.identity_auth import (
    ROLLOUT_NOTE,
    VERDICT_MIGRATE,
    VERDICT_READY,
    VERDICT_WILL_BREAK,
    admins_without_mfa,
    alter_user_stubs,
    auth_readiness,
    classify_auth_readiness,
    with_account_band,
)

_ROOT = Path(__file__).resolve().parents[1]
_NAN = float("nan")


def _row(user_type, pw, mfa=False, logins=0.0, **kw):
    return {"USER_TYPE": user_type, "HAS_PASSWORD": pw, "HAS_MFA": mfa, "PASSWORD_LOGINS_30D": logins, **kw}


@pytest.mark.parametrize(("row", "evidence_ok", "want"), [
    (_row("LEGACY_SERVICE", True, logins=5), True, VERDICT_WILL_BREAK),
    (_row("LEGACY_SERVICE", True, logins=_NAN), True, VERDICT_MIGRATE),
    (_row("LEGACY_SERVICE", True, logins=_NAN), False, VERDICT_WILL_BREAK),   # disuse unproven
    (_row("LEGACY_SERVICE", False), True, VERDICT_MIGRATE),
    (_row(None, True, logins=40), True, VERDICT_WILL_BREAK),                   # unset TYPE = PERSON
    (_row(None, True, logins=0), True, VERDICT_MIGRATE),
    (_row("PERSON", True, logins=40), True, VERDICT_MIGRATE),                  # explicit PERSON never breaks
    (_row("PERSON", True, mfa=True, logins=40), True, VERDICT_READY),
    (_row("PERSON", False), True, VERDICT_READY),
    (_row("SERVICE", True, logins=3), True, VERDICT_READY),
    (_row("SERVICE_AGENT", False), True, VERDICT_READY),
    (_row("SNOWFLAKE_SERVICE", False), True, VERDICT_READY),
])
def test_verdict_matrix(row, evidence_ok, want):
    verdict, reason = classify_auth_readiness(row, evidence_ok=evidence_ok)
    assert verdict == want and reason


def test_auth_readiness_sort_and_evidence_fill():
    frame = pd.DataFrame([
        {**_row("PERSON", False), "USER_NAME": "R1", "IS_ADMIN": True},
        {**_row(None, True, logins=_NAN), "USER_NAME": "M1", "IS_ADMIN": False},
        {**_row("PERSON", True, logins=2), "USER_NAME": "M2", "IS_ADMIN": True},
        {**_row("LEGACY_SERVICE", True, logins=9), "USER_NAME": "W1", "IS_ADMIN": False},
    ])
    ready = auth_readiness(frame, evidence_ok=True)
    assert list(ready["VERDICT"]) == [VERDICT_WILL_BREAK, VERDICT_MIGRATE, VERDICT_MIGRATE, VERDICT_READY]
    assert list(ready["USER_NAME"])[1:3] == ["M2", "M1"]                       # admins first within a verdict
    assert ready.loc[ready["USER_NAME"] == "M1", "PASSWORD_LOGINS_30D"].iloc[0] == 0
    assert ready.loc[ready["USER_NAME"] == "M1", "TYPE_LABEL"].iloc[0] == "PERSON (unset)"
    assert ready.loc[ready["USER_NAME"] == "W1", "ACCOUNT_BAND"].iloc[0] == "Service"
    unknown = auth_readiness(frame, evidence_ok=False)
    assert pd.isna(unknown.loc[unknown["USER_NAME"] == "M1", "PASSWORD_LOGINS_30D"].iloc[0])
    assert auth_readiness(None, evidence_ok=True).empty and auth_readiness(pd.DataFrame(), evidence_ok=True).empty


def test_alter_user_stubs_order_and_safety():
    legacy = pd.DataFrame([{**_row("LEGACY_SERVICE", True, logins=5), "USER_NAME": "ETL_SVC",
                            "HAS_RSA_PUBLIC_KEY": False, "VERDICT": VERDICT_WILL_BREAK}])
    lines = alter_user_stubs(legacy)
    rsa = lines.index("ALTER USER \"ETL_SVC\" SET RSA_PUBLIC_KEY = '<paste the public key>';")
    unset = lines.index('ALTER USER "ETL_SVC" UNSET PASSWORD;')
    svc = lines.index('ALTER USER "ETL_SVC" SET TYPE = SERVICE;')
    assert rsa < unset < svc
    keyed = legacy.assign(HAS_RSA_PUBLIC_KEY=True)
    assert not any("RSA_PUBLIC_KEY" in ln for ln in alter_user_stubs(keyed))
    person = pd.DataFrame([{**_row("PERSON", True), "USER_NAME": "JDOE", "VERDICT": VERDICT_MIGRATE}])
    assert all(ln.startswith("--") for ln in alter_user_stubs(person) if ln)
    ready = pd.DataFrame([{**_row("PERSON", False), "USER_NAME": "OK1", "VERDICT": VERDICT_READY}])
    assert alter_user_stubs(ready) == []
    quoted = legacy.assign(USER_NAME='a"b')
    assert any(ln.startswith('ALTER USER "a""b"') for ln in alter_user_stubs(quoted))
    assert alter_user_stubs(legacy.assign(USER_NAME="bad\nname")) == []


def test_admins_without_mfa_is_null_safe():
    frame = pd.DataFrame([
        {"USER_NAME": "A", "IS_ADMIN": True, "HAS_PASSWORD": True, "HAS_MFA": None},
        {"USER_NAME": "B", "IS_ADMIN": None, "HAS_PASSWORD": True, "HAS_MFA": False},
        {"USER_NAME": "C", "IS_ADMIN": True, "HAS_PASSWORD": True, "HAS_MFA": True},
        {"USER_NAME": "D", "IS_ADMIN": True, "HAS_PASSWORD": True, "HAS_MFA": False},
    ])
    assert list(admins_without_mfa(frame)["USER_NAME"]) == ["A", "D"]


def test_with_account_band_preserves_order_and_none_fallback():
    frame = pd.DataFrame({"USER_NAME": ["b_svc", "alice", "C_SVC"], "X": [1, 2, 3]})
    banded = with_account_band(frame, {"B_SVC", "c_svc"})
    assert list(banded["USER_NAME"]) == ["b_svc", "alice", "C_SVC"]
    assert list(banded["ACCOUNT_BAND"]) == ["Service", "Person", "Service"]
    assert set(with_account_band(frame, None)["ACCOUNT_BAND"]) == {"Person"}
    assert list(with_account_band(pd.DataFrame({"X": [1, 2]}), {"A"})["ACCOUNT_BAND"]) == ["Person", "Person"]


def test_rollout_note_is_dated_from_docs():
    for part in ("Aug–Oct 2026", "PERSON", "SERVICE", "LEGACY_SERVICE", "exact date can differ"):
        assert part in ROLLOUT_NOTE


def test_user_auth_inventory_contract():
    sqlglot = pytest.importorskip("sqlglot")
    sql = security_sql.user_auth_inventory("ALFA")
    for part in ("SNOWFLAKE.ACCOUNT_USAGE.USERS U", "NULLIF(UPPER(TRIM(U.TYPE)), '')", "HAS_RSA_PUBLIC_KEY",
                 "LEFT JOIN password_logins PL",
                 "COUNT(*) OVER () AS TOTAL_CANDIDATES",
                 "SUM(IFF(IS_ADMIN AND HAS_PASSWORD AND NOT HAS_MFA, 1, 0)) OVER () AS ADMIN_PW_NO_MFA_TOTAL",
                 "COMPANY_FOR_USER(U.NAME) = 'ALFA'", "LIMIT 1000"):
        assert part in sql, part
    for role in security_sql.ELEVATED_ROLES:
        assert f"'{role}'" in sql
    assert "LOGIN_HISTORY" not in sql and "POLICY_REFERENCES" not in sql
    sqlglot.parse_one(sql, read="snowflake")


def test_service_users_and_network_policy_contracts():
    sqlglot = pytest.importorskip("sqlglot")
    svc = security_sql.service_users()
    assert "UPPER(U.TYPE) IN ('LEGACY_SERVICE', 'SERVICE', 'SERVICE_AGENT', 'SNOWFLAKE_SERVICE')" in svc
    assert "COMPANY_FOR_USER" not in svc
    npc = security_sql.admin_network_policy_coverage("ALFA")
    for part in ("POLICY_KIND = 'NETWORK_POLICY'", "NP.REF_DOMAIN = 'USER'", "USER_POLICY_REFS",
                 "COMPANY_FOR_USER(GRANTEE_NAME) = 'ALFA'"):
        assert part in npc, part
    sqlglot.parse_one(svc, read="snowflake")
    sqlglot.parse_one(npc, read="snowflake")


def test_security_page_wires_auth_readiness():
    src = (_ROOT / "app/ui/pages/security.py").read_text(encoding="utf-8")
    for part in ("_render_auth_readiness(_auth_inventory(company)", "fact_coverage_complete(legacy_coverage, 30)",
                 "Admins with password and no MFA", "ADMIN_PW_NO_MFA_TOTAL", 'key="sec_admin_netpol_toggle"',
                 "security_sql.service_users()"):
        assert part in src, part
    for helper in ("def _auth_inventory(", "def _render_admin_network_policy(", "def _service_user_names("):
        body = src.split(helper, 1)[1].split("\ndef ", 1)[0]
        assert "probe=True" in body, helper
    assert src.count("ACCOUNT_USAGE") <= 31
    # the evidence-qualified PRIVILEGE COMPLETE rule is an owner decision — untouched
    assert "COUNT(DISTINCT IFF(METRIC = 'BREAKGLASS_GRANTS_30D'" in security_sql.security_domain_coverage()


def test_status_colors_know_readiness_verdicts():
    from app.ui.status_colors import _VERDICTS, status_css
    assert {"WILL BREAK", "MIGRATE", "READY"} <= set(_VERDICTS)
    assert status_css("VERDICT", "WILL BREAK") != ""
