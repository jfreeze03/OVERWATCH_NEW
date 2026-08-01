#!/usr/bin/env python3
r"""Forward-generate V070__delivery_routing_teams_only.sql.

Three delivery defects live in ALREADY-APPLIED migrations (V012, V018), so they need a
FORWARD migration (an applied migration can never be edited). This account is Teams-only:
it uses the notification integration OVERWATCH_WEBHOOK_TEAMS; the Slack-era
OVERWATCH_WEBHOOK integration does NOT exist (see snowflake/webhook_delivery.sql, which
enumerates these same three items as the outstanding Teams-only hazards).

  #23 (P1) SP_DAILY_DIGEST hardcodes the retired Slack integration OVERWATCH_WEBHOOK
      (V018:104) and swallows the send with EXCEPTION WHEN OTHER THEN NULL, so on a
      Teams-only account the morning digest has NEVER been delivered - yet the proc
      returns 'digest written + delivery attempted'. Re-derived from its LATEST def (V018;
      grep confirms only V007/V018 define it and nothing after V018 redefines it) to walk
      the ENABLED rows of ALERT_ROUTES and send through EACH row's INTEGRATION_NAME via
      SYSTEM$SEND_SNOWFLAKE_NOTIFICATION (SP_NOTIFY_WEBHOOK's per-route walk idiom, V034),
      LEDGERING each per-route outcome (a failed send logs a 'digest_send_failed' row to
      APP_ERROR_LOG naming the integration, instead of discarding it). The in-app digest
      write is untouched and stands even if every send fails. Returns a machine-readable
      'digest written; sent N/M routes'.

  #25 (P1) V012 seeds an ENABLED default ALERT_ROUTES row -> OVERWATCH_WEBHOOK, absent on a
      Teams-only account, so every sender cycle writes a route_send_failed that buries real
      errors. An idempotent EXECUTE IMMEDIATE block DISABLES any enabled route whose
      INTEGRATION_NAME is absent from SHOW NOTIFICATION INTEGRATIONS (RESULT_SCAN needs a
      query-id, hence the wrapper). Reversible by re-ENABLING the row.

  #24 (P2) V018's auto-resume gate RESUMEs TASK_ALERT_NOTIFY only when OVERWATCH_WEBHOOK
      exists, so a healthy Teams integration never satisfies it. An idempotent block RESUMEs
      the task when ANY enabled ALERT_ROUTES row names an integration SHOW NOTIFICATION
      INTEGRATIONS confirms exists (evaluated AFTER the #25 disable, so only live routes
      count). Never suspends - a running task is left running.

Derivation law (see gen_v061..69): extract_proc takes the LAST matching definition, _apply
asserts every needle's occurrence count, GUARDS assert nothing load-bearing vanished.
tests/test_v070_delivery_routing.py byte-compares (regenerate via V070_OUT). Idempotent;
apply AFTER V069. The only proc redefined is SP_DAILY_DIGEST; the route-disable and
auto-resume are idempotent data/DDL blocks - no NEW objects.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"

_V018 = "V018__delivery_first_class.sql"


def extract_proc(path: str, name: str) -> str:
    text = (MIG / path).read_text(encoding="utf-8")
    pat = re.compile(
        rf"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.{name}\(.*?\n\$\$;\n", re.S)
    m = pat.findall(text)
    assert m, (path, name)
    return m[-1]


def _apply(body: str, old: str, new: str, count: int, name: str) -> str:
    n = body.count(old)
    assert n == count, f"{name}: needle x{n} (want {count}): {old[:70]!r}"
    return body.replace(old, new)


def derive(name: str) -> str:
    base, edits = EDITS[name]
    body = extract_proc(base, name)
    for old, new, cnt in edits:
        body = _apply(body, old, new, cnt, name)
    for g in GUARDS.get(name, []):
        assert g in body, f"{name}: guardrail vanished: {g[:70]!r}"
    return body


def sql_only(body: str) -> str:
    """The proc with `--` comments stripped, for counting EXECUTED constructs.

    The V070 comments name the retired Slack integration and the bind variables, which
    would otherwise let a real hardcoded target hide behind a comment mention. No string
    literal in this proc contains '--' (the digest banner uses an em-dash), so a line-wise
    split is exact here."""
    return "\n".join(line.split("--")[0] for line in body.splitlines())


EDITS = {
    'SP_DAILY_DIGEST': (_V018, [
        # ---- 1. the route walk needs a cursor over the ENABLED routes plus the N/M
        # counters and the per-route ledger scratch vars.
        ("""DECLARE
    model VARCHAR;
    facts VARCHAR;
    alerts VARCHAR;
    prompt VARCHAR;
    body VARCHAR;
BEGIN""",
         """DECLARE
    model VARCHAR;
    facts VARCHAR;
    alerts VARCHAR;
    prompt VARCHAR;
    body VARCHAR;
    routes_total INT DEFAULT 0;   -- V070 #23: M = enabled routes walked
    routes_sent INT DEFAULT 0;    -- V070 #23: N = routes the digest reached
    emsg VARCHAR;
    r_route_id VARCHAR;
    r_integration VARCHAR;
    c_routes CURSOR FOR
        SELECT r.ROUTE_ID, r.INTEGRATION_NAME
        FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
        WHERE r.ENABLED
        ORDER BY r.ROUTE_ID;
BEGIN""", 1),
        # ---- 2. replace the single hardcoded-integration send (+ blanket WHEN OTHER THEN
        # NULL swallow + false 'delivery attempted' return) with the per-route walk that
        # ledgers each outcome. The DELETE/INSERT into DAILY_DIGEST above is untouched, so
        # the in-app digest is written regardless of any send.
        ("""    -- v2: deliver the narrative through the default webhook route (guarded —
    -- without the integration the digest still writes, just doesn't send).
    BEGIN
        CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
            SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                'OVERWATCH morning digest — ' || TO_VARCHAR(CURRENT_DATE()) || CHR(10) ||
                LEFT(:body, 3000)),
            SNOWFLAKE.NOTIFICATION.INTEGRATION('OVERWATCH_WEBHOOK'));
    EXCEPTION
        WHEN OTHER THEN
            NULL;  -- integration absent/disabled: in-app digest remains the surface
    END;

    RETURN 'digest written + delivery attempted';""",
         """    -- V070 #23: deliver the digest through EVERY enabled ALERT_ROUTES row's own
    -- integration (SP_NOTIFY_WEBHOOK's per-route walk idiom, V034), not the retired
    -- hardcoded Slack integration that does not exist on a Teams-only account. Each
    -- route's outcome is LEDGERED: a failed send logs one 'digest_send_failed' row to
    -- APP_ERROR_LOG naming the integration, replacing the old blanket WHEN OTHER THEN
    -- NULL that hid a never-delivered digest behind a 'delivery attempted' string. The
    -- in-app digest was already written above and stands regardless of any send.
    FOR rec IN c_routes DO
        r_route_id := rec.ROUTE_ID;
        r_integration := rec.INTEGRATION_NAME;
        routes_total := routes_total + 1;
        BEGIN
            CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION(
                SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(
                    'OVERWATCH morning digest — ' || TO_VARCHAR(CURRENT_DATE()) || CHR(10) ||
                    LEFT(:body, 3000)),
                SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration));
            routes_sent := routes_sent + 1;
        EXCEPTION
            WHEN OTHER THEN
                emsg := SQLERRM;
                INSERT INTO DBA_MAINT_DB.OVERWATCH.APP_ERROR_LOG
                    (PAGE, ERROR_TYPE, ERROR_MESSAGE, CONTEXT, ROLE_NAME)
                SELECT 'DailyDigest', 'digest_send_failed', :emsg,
                       'route ' || :r_route_id || ' integration ' || :r_integration ||
                       ' - digest still written in-app; other routes unaffected',
                       CURRENT_ROLE();
        END;
    END FOR;

    RETURN 'digest written; sent ' || :routes_sent || '/' || :routes_total || ' routes';""", 1),
    ]),
}

GUARDS = {
    'SP_DAILY_DIGEST': [
        # the in-app digest write must survive (it is the surface even if all sends fail)
        "DELETE FROM DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST WHERE DIGEST_DATE = CURRENT_DATE();",
        "INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST (DIGEST_DATE, COMPANY, MODEL, BODY)",
        # the Cortex narrative + its own guarded fallback must survive untouched
        "body := SNOWFLAKE.CORTEX.COMPLETE(:model, :prompt);",
        "FROM DBA_MAINT_DB.OVERWATCH.MART_EXEC_BOARD",
    ],
}

digest = derive("SP_DAILY_DIGEST")
code = sql_only(digest)   # comment-free view: counts below reflect EXECUTED SQL

# ---- correctness assertions on the generated proc ----
# the send now walks the enabled routes and targets each route's own integration
assert "FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r" in code, "walks ALERT_ROUTES"
assert "WHERE r.ENABLED" in code, "enabled rows only"
assert "FOR rec IN c_routes DO" in code, "per-route cursor walk"
assert code.count("SYSTEM$SEND_SNOWFLAKE_NOTIFICATION") == 1, "one send, inside the loop"
assert "SNOWFLAKE.NOTIFICATION.INTEGRATION(:r_integration)" in code, "sends via the route's integration"
# the retired hardcoded Slack integration is gone as a send target
assert "INTEGRATION('OVERWATCH_WEBHOOK')" not in code, "no hardcoded integration send target"
assert "'OVERWATCH_WEBHOOK'" not in code, "no literal integration name in executable SQL"
# failures are ledgered, not discarded
assert "'digest_send_failed'" in code, "per-route failure ledgered with a machine ERROR_TYPE"
assert "integration ' || :r_integration" in code, "the failing integration is named in CONTEXT"
assert "WHEN OTHER THEN\n            NULL" not in code, "the blanket swallow is gone"
assert "            NULL;" not in code, "no bare NULL; swallow remains"
# the return string is machine-readable N/M and the false 'delivery attempted' is gone
assert "'digest written; sent ' || :routes_sent || '/' || :routes_total || ' routes'" in code, \
    "machine-readable N/M return"
assert "delivery attempted" not in code, "the false 'delivery attempted' return is gone"
# the in-app digest write + the Cortex narrative are preserved (GUARDS also check this)
assert code.count("INSERT INTO DBA_MAINT_DB.OVERWATCH.DAILY_DIGEST") == 1, "in-app write preserved"
assert code.count("SNOWFLAKE.CORTEX.COMPLETE(:model, :prompt)") == 1, "Cortex narrative preserved"
assert digest.count("CREATE OR REPLACE PROCEDURE") == 1

# ---- #25: idempotent route-disable block (a data UPDATE, no new objects) ----
_ROUTE_DISABLE = """-- V070 #25 (defect): V012 seeded an ENABLED default ALERT_ROUTES row through the
-- Slack-era integration OVERWATCH_WEBHOOK, which does NOT exist on this Teams-only
-- account. Every SP_NOTIFY_WEBHOOK cycle then logs a route_send_failed for it, burying
-- the real Teams delivery errors. DISABLE any enabled route whose INTEGRATION_NAME is
-- absent from the account's live notification integrations. SHOW NOTIFICATION
-- INTEGRATIONS + RESULT_SCAN needs a query-id, so this runs inside EXECUTE IMMEDIATE.
-- Reversible: set ENABLED = TRUE (or create the integration) to bring a route back.
-- Idempotent: a second run finds nothing enabled-but-absent to disable.
EXECUTE IMMEDIATE
$$
BEGIN
    SHOW NOTIFICATION INTEGRATIONS;
    UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES
       SET ENABLED = FALSE
     WHERE ENABLED
       AND UPPER(INTEGRATION_NAME) NOT IN (
           SELECT UPPER("name") FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));
    RETURN 'ok: disabled any enabled route whose integration is absent from the account';
END;
$$;"""

# ---- #24: idempotent auto-resume block (ALTER TASK RESUME, no new objects) ----
_AUTO_RESUME = """-- V070 #24 (defect): V018's auto-resume gate RESUMEd TASK_ALERT_NOTIFY only when the
-- Slack-era OVERWATCH_WEBHOOK integration existed, so a healthy Teams integration never
-- satisfied it and delivery stayed suspended. RESUME the task when ANY enabled
-- ALERT_ROUTES row names an integration that SHOW NOTIFICATION INTEGRATIONS confirms
-- exists (evaluated AFTER the #25 disable above, so only live routes count). This never
-- suspends - a running task is left running. Idempotent: RESUME on a running task is a
-- no-op; if no enabled route resolves to a live integration, the task is left as-is.
EXECUTE IMMEDIATE
$$
DECLARE
    n INT DEFAULT 0;
BEGIN
    SHOW NOTIFICATION INTEGRATIONS;
    SELECT COUNT(*) INTO :n
    FROM DBA_MAINT_DB.OVERWATCH.ALERT_ROUTES r
    WHERE r.ENABLED
      AND UPPER(r.INTEGRATION_NAME) IN (
          SELECT UPPER("name") FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));
    IF (n > 0) THEN
        ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;
        RETURN 'delivery LIVE (' || :n || ' enabled route(s) resolve to a live integration; notify task resumed)';
    END IF;
    RETURN 'no enabled route resolves to a live integration - notify task left as-is';
END;
$$;"""

# both blocks key on live routes, never a literal integration; neither hardcodes a name
for blk in (_ROUTE_DISABLE, _AUTO_RESUME):
    assert "SHOW NOTIFICATION INTEGRATIONS;" in blk
    assert "RESULT_SCAN(LAST_QUERY_ID())" in blk
    assert "'OVERWATCH_WEBHOOK'" not in blk, "the blocks key on live routes, not a literal name"
assert "SET ENABLED = FALSE" in _ROUTE_DISABLE and "NOT IN (" in _ROUTE_DISABLE
assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;" in _AUTO_RESUME
assert "SUSPEND" not in _AUTO_RESUME, "auto-resume never suspends"

out = f"""-- V070__delivery_routing_teams_only.sql
--
-- Three delivery defects in already-applied migrations (V012, V018), fixed forward. This
-- account is Teams-only: it uses OVERWATCH_WEBHOOK_TEAMS; the Slack-era OVERWATCH_WEBHOOK
-- integration does NOT exist (snowflake/webhook_delivery.sql enumerates these same three
-- items as the outstanding Teams-only hazards). Idempotent; apply AFTER V069.
--
--   #23 (P1) SP_DAILY_DIGEST hardcoded the retired Slack integration and swallowed the
--       send with WHEN OTHER THEN NULL, so the morning digest has NEVER been delivered on a
--       Teams-only account while the proc still returned 'delivery attempted'. Re-derived
--       from its latest def (V018) via outputs/gen_v070.py + count-asserted needle edits to
--       walk the ENABLED ALERT_ROUTES rows and send through EACH row's INTEGRATION_NAME
--       (SP_NOTIFY_WEBHOOK's per-route idiom, V034), LEDGERING each per-route outcome to
--       APP_ERROR_LOG ('digest_send_failed' + the integration in CONTEXT) instead of
--       discarding it. The in-app digest write is untouched (it stands even if every send
--       fails); the return is a machine-readable 'digest written; sent N/M routes'.
--   #25 (P1) an idempotent block DISABLES any enabled ALERT_ROUTES row whose integration is
--       absent from SHOW NOTIFICATION INTEGRATIONS (so the dead V012 default Slack route
--       stops writing a route_send_failed every cycle that buries real errors). Reversible.
--   #24 (P2) an idempotent block RESUMEs TASK_ALERT_NOTIFY when any enabled route names an
--       integration SHOW NOTIFICATION INTEGRATIONS confirms exists (a healthy Teams
--       integration now satisfies the gate). Never suspends.
--
-- The only proc re-defined is SP_DAILY_DIGEST; #25/#24 are idempotent data/DDL blocks - no
-- NEW objects. Byte-verified by tests/test_v070_delivery_routing.py. No owner smoke test is
-- required to apply, but delivery is runtime-only, so confirm a real card arrives on the
-- Teams route once (see DEPLOYMENT.md "V070 verify").

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20070, 'V070 requires V069 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 69) THEN
        RAISE not_ready;
    END IF;
END;
$$;

-- >>> derived:SP_DAILY_DIGEST  (route-walk delivery + per-route ledger; no hardcoded integration)
{digest}
-- #25: retire any enabled route whose integration is absent from the account.
{_ROUTE_DISABLE}

-- #24: bring delivery live when an enabled route resolves to a real integration.
{_AUTO_RESUME}

INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)
SELECT 70 AS VERSION,
       'Teams-only delivery routing (V012/V018 forward fix): SP_DAILY_DIGEST hardcoded the retired Slack integration OVERWATCH_WEBHOOK and swallowed the send with WHEN OTHER THEN NULL, so the morning digest had NEVER been delivered on this Teams-only account while it still returned delivery attempted. Re-derived from V018 to walk the enabled ALERT_ROUTES rows and send through each row INTEGRATION_NAME (SP_NOTIFY_WEBHOOK per-route idiom), ledgering each per-route outcome to APP_ERROR_LOG as digest_send_failed instead of discarding it; the in-app digest write is untouched and the return is machine-readable sent N/M routes (#23). An idempotent block disables any enabled route whose integration is absent from SHOW NOTIFICATION INTEGRATIONS so the dead V012 default Slack route stops burying real errors (#25); another resumes TASK_ALERT_NOTIFY when any enabled route names an integration that exists, so a healthy Teams integration satisfies the gate V018 keyed to OVERWATCH_WEBHOOK (#24). Only SP_DAILY_DIGEST is re-defined; the route-disable and auto-resume are idempotent data/DDL blocks - no new objects.' AS DESCRIPTION
WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 70);
"""

assert out.count("CREATE OR REPLACE PROCEDURE") == 1, "one proc re-defined"
assert "CREATE TABLE" not in out and "CREATE TASK" not in out, "no new objects"
assert "CREATE VIEW" not in out and "CREATE STREAM" not in out, "no new objects"
assert "EXCEPTION (-20070" in out and "IF (v < 69) THEN" in out, "version guard"
assert out.count("SHOW NOTIFICATION INTEGRATIONS;") == 2, "route-disable + auto-resume"
assert out.count("EXECUTE IMMEDIATE\n$$") == 3, "version guard + #25 + #24"
assert "ALTER TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_ALERT_NOTIFY RESUME;" in out, "#24 resume"
assert "SUSPEND" not in out, "V070 never suspends the notify task"
assert "SELECT 70 AS VERSION" in out and "WHERE VERSION = 70)" in out, "registry insert"

target = Path(os.environ.get("V070_OUT") or (MIG / "V070__delivery_routing_teams_only.sql"))
target.write_text(out, encoding="utf-8")
print(f"wrote {target} ({len(out)} chars)")
