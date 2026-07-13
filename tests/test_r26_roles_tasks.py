"""r26 locks (owner 2026-07-13): "the only roles that will have access is
SNOW_ACCOUNTADMINS and SNOW_SYSADMINS. remove any traces of other roles.
also remove task monitor references. the app is producing a number of
access error messages."

Two invariants: (1) roles.sql grants to exactly the two SNOW_* roles and
actively retires the old two-role layer; (2) the app holds zero task-history
reads — TASK_HISTORY / TASK_VERSIONS / SERVERLESS_TASK_HISTORY are gone from
every builder and page (the loader's own task DAG lives in frozen migrations
and is monitored through APP_ERROR_LOG + freshness, not these views).
"""

from __future__ import annotations

import inspect
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_roles_sql_grants_only_the_two_snow_roles():
    roles = (_ROOT / "snowflake" / "roles.sql").read_text(encoding="utf-8")
    assert "DROP ROLE IF EXISTS OVERWATCH_OPERATOR;" in roles
    assert "DROP ROLE IF EXISTS OVERWATCH_MONITOR;" in roles
    grantees = {line.split("TO ROLE", 1)[1].strip().rstrip(";")
                for line in roles.splitlines() if "TO ROLE" in line}
    assert grantees == {"SNOW_ACCOUNTADMINS", "SNOW_SYSADMINS"}, grantees
    assert "CREATE ROLE" not in roles                      # no custom layer returns


def test_app_has_no_task_history_reads():
    for py in (_ROOT / "app").rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        for needle in ("ACCOUNT_USAGE.TASK_HISTORY", "TASK_VERSIONS",
                       "SERVERLESS_TASK_HISTORY", "INFORMATION_SCHEMA.TASK_"):
            assert needle not in src, f"{py.name} still references {needle}"


def test_task_surfaces_are_gone_from_the_pages():
    ops = (_ROOT / "app" / "ui" / "pages" / "operations.py").read_text(encoding="utf-8")
    assert '"Tasks"' not in ops and "Task graphs" not in ops
    nav = (_ROOT / "app" / "logic" / "navigate.py").read_text(encoding="utf-8")
    assert '("TASK",' not in nav
    from app.logic.actions import triage_queue
    from app.logic.replay import replay_headlines
    assert "task" not in str(inspect.signature(triage_queue))
    assert "task" not in str(inspect.signature(replay_headlines))


def test_break_glass_panels_watch_the_two_real_roles():
    from app.data import security_sql
    for sql in (security_sql.admin_role_holders(), security_sql.new_network_logins(7)):
        assert "'SNOW_ACCOUNTADMINS', 'SNOW_SYSADMINS'" in sql
        assert "SECURITYADMIN" not in sql and "ORGADMIN" not in sql
