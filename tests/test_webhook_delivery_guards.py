"""snowflake/webhook_delivery.sql is safe to Run All twice (R1-232).

alert_pipeline_check.sql FIX C sent the owner back to this file whenever Teams deliveries failed. Since
02335b74 dropped the old guards, a second Run All (a) minted a SECOND enabled ALERT_ROUTES row for
OVERWATCH_WEBHOOK_TEAMS -- ROUTE_ID is a UUID default and the key is not enforced, and SP_NOTIFY_WEBHOOK /
SP_DAILY_DIGEST deliver per ROUTE_ID, so every alert, digest and escalation posted twice -- and (b) run
unedited, CREATE OR REPLACEd the live secret with the '<REDACTED-...>' placeholder, killing every send.
Now the route insert is NOT EXISTS-guarded, the secret is written only by a block that RAISEs while the
placeholder remains, and FIX C points at the rotation-only ALTER SECRET step.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_WD = (_ROOT / "snowflake" / "webhook_delivery.sql").read_text(encoding="utf-8")


def _statements(text: str) -> list[str]:
    """Top-level statements as Snowsight's Run All sees them: ';' ends one, except inside a 'string',
    a -- comment or a $$ ... $$ scripting block. Comment-only fragments are dropped; leading comment
    lines are stripped so each item starts at its SQL."""
    out, buf, i, n = [], [], 0, len(text)
    in_str = in_cmt = in_dollar = False
    while i < n:
        ch = text[i]
        if in_dollar:
            if text.startswith("$$", i):
                in_dollar = False
                buf.append("$$")
                i += 2
                continue
        elif in_cmt:
            in_cmt = ch != "\n"
        elif in_str:
            in_str = ch != "'"
        elif text.startswith("$$", i):
            in_dollar = True
            buf.append("$$")
            i += 2
            continue
        elif text.startswith("--", i):
            in_cmt = True
        elif ch == "'":
            in_str = True
        elif ch == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    live = []
    for stmt in out:
        lines = [ln for ln in stmt.splitlines() if ln.strip()]
        while lines and lines[0].lstrip().startswith("--"):
            lines.pop(0)
        if lines:
            live.append("\n".join(lines).strip())
    return live


def _routes_db() -> sqlite3.Connection:
    """ALERT_ROUTES with the V012 / V034 / V070 defaults (random ROUTE_ID, nothing enforces a key)."""
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE ALERT_ROUTES (
        ROUTE_ID TEXT NOT NULL DEFAULT (lower(hex(randomblob(16)))),
        FAMILY TEXT NOT NULL, MIN_SEVERITY TEXT NOT NULL DEFAULT 'HIGH',
        INTEGRATION_NAME TEXT NOT NULL, ENABLED INTEGER NOT NULL DEFAULT 1,
        COMPANY_FILTER TEXT DEFAULT 'ALL', DELIVER_DIGEST INTEGER DEFAULT 1)""")
    return db


def _route_inserts() -> list[str]:
    stmts = [s for s in _statements(_WD) if re.match(r"INSERT\s+INTO\s+DBA_MAINT_DB\.OVERWATCH\.ALERT_ROUTES\b", s)]
    assert stmts, "the Teams setup no longer seeds its ALERT_ROUTES row"
    return [s.replace("DBA_MAINT_DB.OVERWATCH.", "") for s in stmts]


def _teams_routes(db: sqlite3.Connection) -> list[tuple]:
    return db.execute("SELECT ROUTE_ID, ENABLED FROM ALERT_ROUTES "
                      "WHERE INTEGRATION_NAME = 'OVERWATCH_WEBHOOK_TEAMS'").fetchall()


def test_running_the_setup_twice_keeps_one_teams_route():
    db = _routes_db()
    for _run in range(2):                                     # first-time setup, then FIX C's re-run
        for stmt in _route_inserts():
            db.execute(stmt)
    routes = _teams_routes(db)
    assert len(routes) == 1, f"a re-run minted {len(routes)} Teams routes -- every card would post {len(routes)}x"


def test_a_deliberately_disabled_route_is_not_re_added():
    db = _routes_db()
    db.execute("INSERT INTO ALERT_ROUTES (FAMILY, INTEGRATION_NAME, ENABLED) "
               "VALUES ('ALL', 'OVERWATCH_WEBHOOK_TEAMS', 0)")
    for stmt in _route_inserts():
        db.execute(stmt)
    assert [enabled for _rid, enabled in _teams_routes(db)] == [0]


def test_an_unedited_run_aborts_before_touching_the_secret_or_integration():
    stmts = _statements(_WD)
    # nothing at top level writes the secret: only the guarded block may
    assert not [s for s in stmts if re.match(r"CREATE\s+(OR\s+REPLACE\s+)?SECRET\b", s, re.I)]
    guard_at = next(i for i, s in enumerate(stmts)
                    if s.startswith("EXECUTE IMMEDIATE $$") and "OVERWATCH_TEAMS_URL" in s)
    guard = stmts[guard_at]
    default = re.search(r"teams_secret VARCHAR DEFAULT '([^']*)';", guard).group(1)
    raise_at = guard.index("RAISE placeholder_still_present;")
    assert guard.index("IF (CONTAINS(teams_secret, '<')) THEN") < raise_at
    assert raise_at < guard.index("CREATE OR REPLACE SECRET DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL")
    # Run All halts at the first failing statement: as committed, the guard raises, and every
    # statement that recreates the integration comes after it.
    assert "<" in default and default == "<REDACTED-PASTE-IN-SNOWSIGHT>"
    integration_at = next(i for i, s in enumerate(stmts)
                          if s.startswith("CREATE OR REPLACE NOTIFICATION INTEGRATION OVERWATCH_WEBHOOK_TEAMS"))
    assert guard_at < integration_at
    # the secret value is escaped into the dynamic DDL, so a quote in it cannot break out, and the DDL
    # runs through a variable (EXECUTE IMMEDIATE :ddl -- the repo idiom), not an inline expression
    assert "REPLACE(teams_secret, '''', '''''')" in guard
    assert guard.index("ddl := 'CREATE OR REPLACE SECRET") < guard.index("EXECUTE IMMEDIATE :ddl;")


def test_fix_c_points_at_the_rotation_step_not_a_full_re_run():
    apc = (_ROOT / "snowflake" / "alert_pipeline_check.sql").read_text(encoding="utf-8")
    fix_c = apc.split("-- FIX C", 1)[1].split("-- ====", 1)[0]
    assert "recreate the\n-- Teams integration per" not in fix_c
    assert "ROTATION RUNBOOK" in fix_c and "ALTER SECRET" in fix_c
    # the runbook exists, and is a commented recipe (a rotation is pasted in Snowsight, never here)
    assert "-- ROTATION RUNBOOK" in _WD
    assert "--   ALTER SECRET DBA_MAINT_DB.OVERWATCH.OVERWATCH_TEAMS_URL" in _WD
    assert not [s for s in _statements(_WD) if s.upper().startswith("ALTER SECRET")]
    # DEPLOYMENT.md cites "webhook_delivery.sql's rotation runbook" -- it resolves again
    assert "webhook_delivery.sql's rotation runbook" in (_ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
