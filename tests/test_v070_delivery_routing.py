"""Locks for V070 — Teams-only delivery routing (V012/V018 forward fix).

SP_DAILY_DIGEST hardcoded the retired Slack integration OVERWATCH_WEBHOOK and swallowed
its send with EXCEPTION WHEN OTHER THEN NULL, so on a Teams-only account the morning
digest had NEVER been delivered while the proc still returned 'delivery attempted'. The
proc is re-derived from its latest def (V018) to walk the ENABLED ALERT_ROUTES rows and
send through each row's own INTEGRATION_NAME (SP_NOTIFY_WEBHOOK's per-route idiom, V034),
ledgering each per-route outcome. Two idempotent blocks disable any enabled route whose
integration is absent (#25) and resume TASK_ALERT_NOTIFY when an enabled route resolves to
a live integration (#24).

The generator is the source of truth: the first test regenerates and byte-compares.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_V70 = (_MIG / "V070__delivery_routing_teams_only.sql").read_text(encoding="utf-8")


def _proc(text: str, name: str) -> str:
    m = re.findall(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", text, re.S)
    assert m, name
    return m[-1]


def _code(body: str) -> str:
    """`body` with `--` comments stripped — counts must reflect EXECUTED SQL, not prose
    (the V070 comments name the retired Slack integration and the bind variables)."""
    return "\n".join(line.split("--")[0] for line in body.splitlines())


_DIGEST = _proc(_V70, "SP_DAILY_DIGEST")
_SQL = _code(_DIGEST)


def test_v070_regenerates_byte_identical(tmp_path):
    out = tmp_path / "regen.sql"
    r = subprocess.run([sys.executable, str(_ROOT / "outputs" / "gen_v070.py")],
                       env={**os.environ, "V070_OUT": str(out)}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == _V70, (
        "V070 drifted from outputs/gen_v070.py — regenerate, never hand-edit.")


def test_v070_guard_version_and_no_new_objects():
    assert "EXCEPTION (-20070" in _V70 and "IF (v < 69) THEN" in _V70
    assert "SELECT 70 AS VERSION" in _V70 and "WHERE VERSION = 70)" in _V70
    # only SP_DAILY_DIGEST is re-defined
    assert _V70.count("CREATE OR REPLACE PROCEDURE") == 1
    assert "SP_DAILY_DIGEST" in _V70
    # no new tables, tasks, views or streams — #25/#24 are data/DDL blocks
    for ddl in ("CREATE TABLE", "CREATE TRANSIENT TABLE", "CREATE TASK",
                "CREATE VIEW", "CREATE OR REPLACE VIEW", "CREATE STREAM"):
        assert ddl not in _V70, ddl


def test_digest_walks_alert_routes_with_no_hardcoded_integration():
    # walks the ENABLED routes via a cursor and sends through each route's own integration
    assert "FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r" in _SQL
    assert "WHERE r.ENABLED" in _SQL
    assert "FOR rec IN c_routes DO" in _SQL
    assert _SQL.count("SYSTEM$SEND_SNOWFLAKE_NOTIFICATION") == 1, "one send, inside the loop"
    assert "SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration)" in _SQL
    # the retired Slack integration is no longer a send target anywhere in executable SQL
    assert "INTEGRATION('OVERWATCH_WEBHOOK')" not in _SQL
    assert "'OVERWATCH_WEBHOOK'" not in _SQL, "no literal integration name in the proc's SQL"


def test_digest_ledgers_each_route_failure_no_bare_swallow():
    # a failed send logs a machine-readable row naming the integration, not a discard
    assert "'digest_send_failed'" in _SQL
    assert "INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG" in _SQL
    assert "integration ' || :r_integration" in _SQL, "the failing integration is in CONTEXT"
    # the old blanket swallow is gone (the surviving WHEN OTHER sets body / logs a row)
    assert "WHEN OTHER THEN\n            NULL" not in _SQL
    assert "            NULL;" not in _SQL


def test_digest_still_writes_in_app_and_returns_machine_readable_n_of_m():
    # the in-app digest write survives — it is the surface even if every send fails
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST WHERE DIGEST_DATE = CURRENT_DATE();" in _SQL
    assert _SQL.count("INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST") == 1
    assert _SQL.count("SNOWFLAKE.CORTEX.COMPLETE(:model, :prompt)") == 1
    # machine-readable N/M return; the false 'delivery attempted' string is gone
    assert ("RETURN 'digest written; sent ' || :routes_sent || '/' || :routes_total || ' routes'"
            in _SQL)
    assert "delivery attempted" not in _SQL


def test_route_disable_block_present_and_idempotent():
    # #25: SHOW NOTIFICATION INTEGRATIONS + RESULT_SCAN inside EXECUTE IMMEDIATE (needs a
    # query-id), disabling any enabled route whose integration the account does not have.
    block = _V70.split("#25:", 1)[1].split("#24:", 1)[0]
    assert "SHOW NOTIFICATION INTEGRATIONS;" in block
    assert "UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES" in block
    assert "SET ENABLED = FALSE" in block
    assert 'UPPER(INTEGRATION_NAME) NOT IN (' in block
    assert "RESULT_SCAN(LAST_QUERY_ID())" in block
    # keyed on the live integration set, never a hardcoded name (idempotent + reversible)
    assert "'OVERWATCH_WEBHOOK'" not in block


def test_auto_resume_keys_on_enabled_routes_not_a_literal_integration():
    # #24: RESUME when any ENABLED route resolves to a live integration; never suspend.
    block = _V70.split("#24:", 1)[1]
    assert "SHOW NOTIFICATION INTEGRATIONS;" in block
    assert "FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r" in block
    assert "WHERE r.ENABLED" in block
    assert "UPPER(r.INTEGRATION_NAME) IN (" in block
    assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;" in block
    assert "'OVERWATCH_WEBHOOK'" not in block, "gate keys on enabled routes, not a literal"
    assert "SUSPEND" not in block, "auto-resume never suspends a running task"


def test_v070_never_suspends_the_notify_task():
    assert "SUSPEND" not in _V70


def test_v070_39_replaces_todays_digest_atomically():
    # #39: the DELETE+INSERT replace of today's digest is wrapped in ONE explicit
    # transaction, so an autocommit crash between the two statements can no longer leave
    # today's digest row blank; on any error it rolls back the prior row and re-raises.
    assert "BEGIN TRANSACTION;" in _SQL
    assert _SQL.count("COMMIT;") == 1, "the digest replace commits exactly once"
    assert "ROLLBACK;\n            RAISE;" in _SQL, "rollback + re-raise, never a blank digest"
    # both writes still live, now inside the guarded transaction
    assert "DELETE FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST WHERE DIGEST_DATE = CURRENT_DATE();" in _SQL
    assert _SQL.count("INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST") == 1


def test_v070_11_digest_targets_only_digest_eligible_routes():
    # #11: an additive, backward-compatible column gates digest eligibility; the digest
    # cursor filters on it so a future non-digest (PagerDuty/tactical) route is never sent
    # executive prose. Existing routes DEFAULT TRUE, so today's behavior is unchanged.
    assert "ALTER TABLE IF EXISTS DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES" in _V70
    assert "ADD COLUMN IF NOT EXISTS DELIVER_DIGEST BOOLEAN NOT NULL DEFAULT TRUE;" in _V70
    assert "WHERE r.ENABLED AND r.DELIVER_DIGEST" in _SQL, "digest cursor is DELIVER_DIGEST-gated"
    # additive only — not a new object (the no-new-objects guard also covers CREATE TABLE)
    assert "CREATE TABLE" not in _V70 and "CREATE OR REPLACE TABLE" not in _V70


def test_v070_12_all_failed_digest_is_loud_not_silent_success():
    # #12: previously an all-failed run only logged per-route failures and still returned a
    # bland 'sent 0/M' string. Now, when routes were eligible but NONE delivered, a loud
    # 'digest_undelivered' row is logged and the return string is marked, so it is observable.
    assert "'digest_undelivered'" in _SQL
    assert "IF (routes_total > 0 AND routes_sent = 0) THEN" in _SQL
    assert _SQL.count("INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG") == 2, \
        "per-route digest_send_failed + the all-failed digest_undelivered"
    assert "' [UNDELIVERED]'" in _SQL, "the zero-success case is loud in the return string too"


def test_v070_in_migration_registry():
    # V070 is registered in the admin contract. (The validate.sql floor is the MOVING tip,
    # owned by tests/test_v451_trust.py + tests/test_perf_budgets.py — pinning it here would
    # fail the moment V071 lands.)
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 70 in _EXPECTED_MIGRATIONS
