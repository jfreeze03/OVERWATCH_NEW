"""Locks for the 2026-07-09 live findings, round 4 (v4.11.0).

Day replay obeys the company scope on EVERY metric; the credential-expiry
policy is 10 days end to end (V028: rule threshold + posture bucket, proc
derived VERBATIM from V027); Spend trend reads as daily bars + 7d average
with an honest partial-day dim; Security Changes stacks by change kind next
to who made them; the driver inventory names drivers, versions, programs.
"""

from __future__ import annotations

from pathlib import Path

from app.data import mart_sql, security_sql

_ROOT = Path(__file__).resolve().parents[1]
_MIG27 = (_ROOT / "snowflake" / "migrations" / "V027__mart_family.sql").read_text(encoding="utf-8")
_MIG28 = (_ROOT / "snowflake" / "migrations" / "V028__cred_expiry_10d.sql").read_text(encoding="utf-8")
_CHARTS = (_ROOT / "app" / "ui" / "charts.py").read_text(encoding="utf-8")
_SECURITY = (_ROOT / "app" / "ui" / "pages" / "security.py").read_text(encoding="utf-8")
_CR = (_ROOT / "app" / "ui" / "pages" / "control_room.py").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Day replay — company scope on every metric (the ALFA/Trexis leak, round 4)
# ---------------------------------------------------------------------------

def test_replay_builders_take_company():
    mv = mart_sql.day_spend_movers("2026-07-08", "ALFA")
    assert mv.count("COMPANY = 'ALFA'") == 2                  # day CTE + baseline CTE
    act = mart_sql.day_activity("2026-07-08", "ALFA")
    assert act.count("COMPANY = 'ALFA'") == 2                 # day + its 14d baseline
    assert "(COMPANY = 'ALFA' OR UPPER(COMPANY) = 'ALL')" in mart_sql.day_alerts("2026-07-08", "ALFA")
    # account-wide replay stays account-wide
    for fn in (mart_sql.day_spend_movers, mart_sql.day_activity,
               mart_sql.day_alerts):
        assert "COMPANY = '" not in fn("2026-07-08")


def test_replay_call_sites_pass_the_scope():
    body = _CR.split("def _day_replay", 1)[1].split("\ndef ", 1)[0]
    for call in ("day_spend_movers(day_iso, rp_company)", "day_activity(day_iso, rp_company)",
                 "day_alerts(day_iso, rp_company)"):
        assert body.count(call) == 2, call                    # batch spec + serial fallback
    assert "Scoped to {rp_company}" in body                   # the caption says so


# ---------------------------------------------------------------------------
# V028 — credential expiry 10d (rule + posture bucket)
# ---------------------------------------------------------------------------

def test_v028_guard_version_and_rule_update():
    assert "EXCEPTION (-20028" in _MIG28
    assert "IF (v < 27) THEN" in _MIG28
    assert "SELECT 28 AS VERSION" in _MIG28
    assert "SET THRESHOLD_NUM = 10" in _MIG28
    assert "WHERE RULE_ID = 'SEC_CRED_EXPIRY'" in _MIG28
    assert "CREATE TABLE" not in _MIG28.upper()               # policy change, not surgery


def _proc(text: str) -> str:
    start = text.find("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_LOAD_MARTS_V27")
    assert start > 0
    open_dd = text.find("$$", start)
    return text[start:text.find("$$;", open_dd + 2) + 3]


def test_v028_proc_is_v027_verbatim_except_the_bucket():
    # The replacement proc is DERIVED, not hand-copied — this equality is the
    # anti-drift contract. Editing V027's proc without regenerating V028
    # (or vice versa) fails here, loudly.
    expected = (
        _proc(_MIG27)
        .replace("'EXPIRING_CRED_30D' AS METRIC", "'EXPIRING_CRED_10D' AS METRIC")
        .replace("AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 30, CURRENT_TIMESTAMP())",
                 "AND EXPIRATION_DATE BETWEEN CURRENT_TIMESTAMP() AND DATEADD('day', 10, CURRENT_TIMESTAMP())")
    )
    assert _proc(_MIG28) == expected


def test_ten_day_horizon_everywhere_app_side():
    assert _SECURITY.count("expiring_credentials(10, company)") == 3   # batch + serial + pack
    assert "Expiring ≤10d" in _SECURITY and "≤30d" not in _SECURITY
    assert "(10-day horizon)" in _SECURITY
    assert "expiring_credentials_10d" in _SECURITY                     # pack sheet renamed
    sqlsrc = (_ROOT / "app" / "data" / "security_sql.py").read_text(encoding="utf-8")
    assert "DATEADD('day', 10, CURRENT_TIMESTAMP())) AS EXPIRING_CREDENTIALS" in sqlsrc
    gov = (_ROOT / "app" / "logic" / "governance.py").read_text(encoding="utf-8")
    assert "expire within 10 days" in gov


# ---------------------------------------------------------------------------
# Client drivers & versions (Security -> Clients)
# ---------------------------------------------------------------------------

def test_client_drivers_builder_shape():
    sql = security_sql.client_drivers(30, "ALFA")
    assert chr(92) not in sql                                 # POSIX classes only (CALLs+ lesson)
    assert "REGEXP_SUBSTR(CLIENT_APPLICATION_ID, '[0-9][0-9.]*$')" in sql
    assert "TRY_PARSE_JSON(CLIENT_ENVIRONMENT):APPLICATION::STRING" in sql
    assert "COMPANY_FOR_USER" in sql                          # user-grain company scoping
    assert "FIRST_VALUE(VERSION) OVER (PARTITION BY DRIVER ORDER BY VKEY DESC)" in sql
    assert "'BEHIND'" in sql and "'CURRENT'" in sql
    assert sql.count("LPAD(") == 4                            # 3.10.2 > 3.9.1, per segment
    assert "-90," in security_sql.client_drivers(9999)        # window clamped
    assert "COMPANY_FOR_USER" not in security_sql.client_drivers(30, "ALL")


def test_clients_panel_wired_and_canaried():
    # r25 superseded the 4-section list: owner picked #7 (egress watch), which
    # earned its own lazy section between Clients and Trust Center.
    assert 'lazy_sections(["Access", "Changes", "Clients", "Egress", "Trust Center"]' in _SECURITY
    assert "ACCOUNT_USAGE.SESSIONS" in _SECURITY
    assert "sec_drivers_csv" in _SECURITY                     # inventory is exportable
    assert "(not reported)" in _SECURITY                      # honest PROGRAM caveat
    canary = (_ROOT / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    assert "security_sql.client_drivers" in canary
    assert "security_sql.expiring_credentials(10," in canary  # canary follows the policy
    colors = (_ROOT / "app" / "ui" / "status_colors.py").read_text(encoding="utf-8")
    assert '"BEHIND": _WARN' in colors and '"CURRENT": _OK' in colors


# ---------------------------------------------------------------------------
# Spend trend redesign — bars + 7d average, honest partial day
# ---------------------------------------------------------------------------

def test_spend_trend_is_bars_plus_average_not_wash():
    seg = _CHARTS.split("def spend_trend", 1)[1].split("def bar_usd", 1)[0]
    assert "mark_bar" in seg and "rolling(7" in seg
    assert "PROVISIONAL" in seg and "0.45" in seg             # newest partial day dims
    assert "mark_area" not in seg                             # the unreadable wash is gone
    assert "partial, not a drop" in seg                       # caption answers the question
    assert "band" not in seg                                  # forecast rect retired


def test_spend_trend_callers_dropped_the_band():
    ov = (_ROOT / "app" / "ui" / "pages" / "overview.py").read_text(encoding="utf-8")
    assert "spend_trend(daily, daily_budget_usd=daily_budget)" in ov
    stress = (_ROOT / "tests" / "test_stress.py").read_text(encoding="utf-8")
    assert "band=" not in stress.split("spend_trend", 1)[1].split("\n", 1)[0]


# ---------------------------------------------------------------------------
# Security Changes redesign — kind-stacked days beside who
# ---------------------------------------------------------------------------

def test_changes_chart_stacks_kind_beside_who():
    body = _SECURITY.split("def _changes_tab", 1)[1].split("\ndef ", 1)[0]
    assert "daily_stacked_count" in body and "CHANGE_KIND" in body
    assert "bar_count(by_user" in body                        # the who, right beside
    assert "daily_count_bars" not in body                     # flat total retired
    assert "def daily_stacked_count" in _CHARTS


def test_change_kind_families():
    src = _SECURITY.split("def _change_kind", 1)[1].split("\ndef ", 1)[0]
    ns: dict = {}
    exec("def _change_kind" + src, ns)                        # pure function — safe to exec
    ck = ns["_change_kind"]
    assert ck("CREATE_TABLE_AS_SELECT") == "Create"
    assert ck("ALTER_WAREHOUSE_SUSPEND") == "Alter"
    assert ck("TRUNCATE_TABLE") == "Drop / truncate"
    assert ck("REVOKE") == "Grants"
    assert ck("USE") == "Other" and ck(None) == "Other"
