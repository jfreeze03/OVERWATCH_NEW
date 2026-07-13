"""r27 locks — identity, executor allow-list, domain invalidation, bulk ack,
admin self-check, docs drift. Authority: CODEX_R27_ADJUDICATION_20260713.md.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_viewer_identity_is_threaded_not_current_user():
    # every per-user write/read rides identity_sql(); raw CURRENT_USER()
    # survives only as the in-expression fallback + the session probe.
    from app.core.identity import identity_sql, viewer_name
    assert viewer_name() == ""                      # headless: no st.user
    assert identity_sql() == "CURRENT_USER()"       # honest fallback
    for rel in ("app/data/prefs_sql.py", "app/ui/pages/alerts.py",
                "app/ui/pages/admin.py", "app/ui/pages/cost_parts/ai_chargeback.py",
                "app/ui/pages/cost_parts/optimize.py", "app/ui/components.py",
                "app/main.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "identity_sql" in src, rel
    prefs = (_ROOT / "app" / "data" / "prefs_sql.py").read_text(encoding="utf-8")
    assert "USER_NAME = CURRENT_USER()" not in prefs
    # cache isolation follows the viewer too
    q = (_ROOT / "app" / "core" / "query.py").read_text(encoding="utf-8")
    assert "viewer_name() or str(st.session_state.get(\"_ow_current_user\"" in q


def test_executor_enforces_the_allow_list():
    from app.core.query import _statement_allowed
    ok, _ = _statement_allowed("UPDATE DBA_MAINT_DB.OVERWATCH.SETTINGS SET VALUE = '1' WHERE KEY = 'X';")
    assert ok
    ok, _ = _statement_allowed("ALTER WAREHOUSE WH_X SUSPEND;")
    assert ok
    ok, why = _statement_allowed("DROP TABLE DBA_MAINT_DB.OVERWATCH.SETTINGS;")
    assert not ok and "allow-list" in why
    ok, why = _statement_allowed("SELECT 1; SELECT 2;")
    assert not ok and "one statement" in why
    # semicolons inside string literals are NOT multi-statement
    ok, _ = _statement_allowed(
        "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT (NOTE) VALUES ('a;b');")
    assert ok
    ok, _ = _statement_allowed("CREATE TABLE DBA_MAINT_DB.OVERWATCH.X (A INT);")
    assert not ok


def test_writes_invalidate_domains_not_the_world():
    from app.core.query import _domains_in
    assert _domains_in("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS SET X=1") == ["alerts"]
    assert _domains_in("MERGE INTO DBA_MAINT_DB.OVERWATCH.USER_PREFS t USING x") == ["prefs"]
    assert _domains_in("ALTER WAREHOUSE WH_X SUSPEND") == []        # unknown -> global bump
    q = (_ROOT / "app" / "core" / "query.py").read_text(encoding="utf-8")
    assert "_bump_refresh(sql)" in q
    assert "_cache_scope(sql)" in q                                  # reads carry domain salts


def test_bulk_ack_is_two_statements_not_2n():
    alerts = (_ROOT / "app" / "ui" / "pages" / "alerts.py").read_text(encoding="utf-8")
    assert "def _bulk_lifecycle_sql(" in alerts
    assert "EVENT_ID IN (" in alerts
    bulk = alerts.split("Bulk acknowledge / resolve", 1)[1]
    assert "for label in chosen" not in bulk                         # the loop is gone
    assert "_bulk_lifecycle_sql([options[c] for c in chosen]" in bulk


def test_admin_gains_self_check_error_grouping_settings_hygiene():
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert "def _access_self_check(" in adm
    assert "TRUST_CENTER_VIEWER" in adm                              # fix SQL in hand
    assert "Settings rows the app no longer reads" in adm            # H2
    assert "FIRST_SEEN=(\"LOGGED_AT\", \"min\")" in adm              # H4 grouping


def test_docs_describe_the_two_role_owner_rights_model():
    dep = (_ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    run = (_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "owner's-rights service" in dep
    assert "st.user" in dep
    for doc, name in ((dep, "DEPLOYMENT.md"), (run, "RUNBOOK.md")):
        body = doc.replace("retires the old\nOVERWATCH_MONITOR / OVERWATCH_OPERATOR layer", "") \
                  .replace("the old monitor/operator layer is\n  retired", "")
        assert "GRANT ROLE OVERWATCH" not in body, name
        assert "viewers hold **OVERWATCH_MONITOR**" not in body, name
    roles = (_ROOT / "snowflake" / "roles.sql").read_text(encoding="utf-8")
    assert roles.count("REVOKE UPDATE, DELETE ON TABLE DBA_MAINT_DB.OVERWATCH.ALERT_AUDIT") == 2
    assert roles.count("REVOKE UPDATE, DELETE ON TABLE DBA_MAINT_DB.OVERWATCH.REMEDIATION_LOG") == 2
    assert "SHOW GRANTS ON STREAMLIT" in roles                       # #7 proof block
