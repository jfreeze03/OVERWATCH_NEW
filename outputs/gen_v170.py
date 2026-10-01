#!/usr/bin/env python3
"""Forward-generate V170: the manual incident declare credits the DBA, never commits an empty incident, and the
proposal view classifies today's rules (R2-028, R2-030, R2-093, R2-031).

Reads ONLY the two current definers (tests/test_proc_lineage.py: nothing after them re-defines either object):
  * V131__incident_declare_atomic.sql            -- SP_INCIDENT_DECLARE(VARCHAR x4), its only CREATE;
  * V072__entity_aware_incident_proposals.sql    -- INCIDENT_PROPOSALS (V032's text is stale).
and emits, in order:

  guard (-20170, v < 169) -> marker + SP_INCIDENT_DECLARE 4-arg (re-derived from V131) -> marker +
  SP_INCIDENT_DECLARE 5-arg (a NEW overload, derived from V131) -> marker + INCIDENT_PROPOSALS (re-derived from
  V072) -> SCHEMA_VERSION 170.

SP_INCIDENT_DECLARE deltas (BOTH overloads; each asserted by count, everything else byte-identical to V131):
  R1  R2-030: after the two COUNT(*) INTO reads and before COMMIT, a created incident with 0 members ROLLBACKs
      and returns 'NOOP: no open alerts left to link - nothing declared' (the proposal list is up to 30 s stale:
      an auto-clear / condition-ended sweep, another operator or a concurrent auto-declare can take every alert
      out of the linkable set first)
  R2  R2-030: the success verdict 'DECLARED: <n> member(s) linked' -> 'OK: declared <id> with <n> member(s)
      linked' (the house execute_action verdict allowlist is OK / VERIFIED / DUPLICATE)
5-arg overload only (R2-028), on top of R1 + R2 -- the two bodies differ ONLY in attribution:
  A1  + P_ACTOR VARCHAR (no DEFAULT: an optional arg would be an ambiguous overload of the kept 4-arg)
  A2  + v_actor STRING declare
  A3  v_actor := LEFT(COALESCE(NULLIF(TRIM(:P_ACTOR), ''), CURRENT_USER()), 200) after the UUID
  A4  INCIDENTS insert writes DECLARED_BY = :v_actor
  A5  INCIDENT_MEMBERS insert writes LINKED_BY = :v_actor
The 4-arg overload is KEPT (no DROP): an app not yet redeployed still CALLs it, and the deployed app selects the
5-arg CALL only once has_migration(170). A later migration drops the 4-arg once the gated app is live.

INCIDENT_PROPOSALS deltas (each asserted by count, everything else byte-identical to V072):
  P1  R2-093 WAREHOUSE += COST_IDLE_OPPORTUNITY, COST_SLEEP_POLLING (warehouse in key part 2)
  P2  R2-093 OBJECT += PIPE_DT_FAILURES, PIPE_VOLUME_DROP, DQ_BREACH, DQ_SCHEMA_DRIFT (object FQN in part 2)
  P3  R2-093 USER += SEC_FAILED_LOGINS, SEC_LOGIN_TAKEOVER, SEC_ADMIN_GRANT, COST_AI_USER_RUNAWAY (user in part 2)
  P4  R2-093 ACCOUNT band tokens += 'EXH', 'ALL' (an EXH band no longer reads as an entity and opens a second
      incident for the same family; 'ALL' is COST_DAILY_CREDITS / COST_BUDGET_PACE / COST_FORECAST_BREACH)
  P5  R2-031 CONFIDENCE: a PIPE_TASK_FAILURES proposal's own FACT_TASK_DAILY failures are its alert source, not
      corroboration -> excluded from the HIGH test
  P6  R2-031 EVIDENCE labels that count 'task failures (alert source, not corroboration)='
The column set and order are unchanged; no COPY GRANTS (as V072: the roles.sql FUTURE VIEWS grants apply).

No CALL, DROP, task, ALTER or data write at apply time. With PREFLIGHT_OUT set, also writes the read-only
PREFLIGHT section (P170.1-P170.3: the V170 view body run as a SELECT, built from the SAME view text); with
PART_B_OUT set, the read-only RUN_NEXT PART B verify grids (V170.1-V170.6). The byte-identity test never sets either.

Run: python outputs/gen_v170.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / "snowflake" / "migrations"
V131 = (MIG / "V131__incident_declare_atomic.sql").read_text(encoding="utf-8")
V072 = (MIG / "V072__entity_aware_incident_proposals.sql").read_text(encoding="utf-8")
NAME = "V170__incident_declare_actor_and_proposals.sql"


def extract_proc(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    open_dd = text.index("$$", start)
    return text[start:text.index("$$;", open_dd + 2) + 3]


def extract_view(text: str, name: str) -> str:
    start = text.index(f"CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.{name} AS\n")
    end_tok = "\nFROM evidence;\n"
    return text[start:text.index(end_tok, start) + len(end_tok)]


def _swap(text: str, old: str, new: str, label: str, n: int = 1) -> str:
    assert text.count(old) == n, f"{label}: expected {n} anchor(s), got {text.count(old)}"
    return text.replace(old, new)


BASE_PROC = extract_proc(V131, "SP_INCIDENT_DECLARE(")
BASE_VIEW = extract_view(V072, "INCIDENT_PROPOSALS")
assert V131.count("CREATE OR REPLACE PROCEDURE") == 1 and V072.count("CREATE OR REPLACE VIEW") == 1

# ---------------------------------------------------------------------------------------------------------------
# SP_INCIDENT_DECLARE: R1 + R2 (both overloads)
# ---------------------------------------------------------------------------------------------------------------
R1_OLD = ("    SELECT COUNT(*) INTO :members FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS WHERE INCIDENT_ID = :inc_id;\n"
          "\n"
          "    COMMIT;\n")
R1_NEW = ("    SELECT COUNT(*) INTO :members FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS WHERE INCIDENT_ID = :inc_id;\n"
          "\n"
          "    -- V170 (R2-030): never commit a member-less incident. The proposal the operator declared from can be up\n"
          "    -- to 30 s stale, and an auto-clear or condition-ended sweep, another operator resolving the alerts, or a\n"
          "    -- concurrent auto-declare can take every proposal alert out of OPEN/ACK (or into another incident)\n"
          "    -- before this CALL; the members INSERT then links 0 rows. Undo the incident row (it was never visible\n"
          "    -- outside this transaction) and say so.\n"
          "    IF (:created = 1 AND :members = 0) THEN\n"
          "        ROLLBACK;\n"
          "        RETURN 'NOOP: no open alerts left to link - nothing declared';\n"
          "    END IF;\n"
          "\n"
          "    COMMIT;\n")
R2_OLD = "    RETURN 'DECLARED: ' || :members || ' member(s) linked';\n"
R2_NEW = "    RETURN 'OK: declared ' || :inc_id || ' with ' || :members || ' member(s) linked';\n"

# ---------------------------------------------------------------------------------------------------------------
# SP_INCIDENT_DECLARE 5-arg: A1-A5 (R2-028)
# ---------------------------------------------------------------------------------------------------------------
A1_OLD = "    P_PROPOSAL_KEY VARCHAR\n)\n"
A1_NEW = "    P_PROPOSAL_KEY VARCHAR,\n    P_ACTOR VARCHAR\n)\n"
A2_OLD = "    inc_id STRING;\n"
A2_NEW = "    inc_id STRING;\n    v_actor STRING;\n"
A3_OLD = "    inc_id := UUID_STRING();\n"
A3_NEW = ("    inc_id := UUID_STRING();\n"
          "    -- V170 (R2-028): the DBA who declared. The app passes identity_sql() (st.user under owner-rights SiS);\n"
          "    -- CURRENT_USER() there is the app owner, so it is only the fallback. LEFT(..., 200) = the width of\n"
          "    -- INCIDENTS.DECLARED_BY and INCIDENT_MEMBERS.LINKED_BY (V032).\n"
          "    v_actor := LEFT(COALESCE(NULLIF(TRIM(:P_ACTOR), ''), CURRENT_USER()), 200);\n")
A4_OLD = ("        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, ROOT_CAUSE_KIND)\n"
          "    SELECT :inc_id, LEFT(:P_TITLE, 300), UPPER(:P_SEVERITY), 'OPEN', :P_COMPANY,\n"
          "           CURRENT_TIMESTAMP(), 'UNKNOWN'\n")
A4_NEW = ("        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, ROOT_CAUSE_KIND, DECLARED_BY)\n"
          "    SELECT :inc_id, LEFT(:P_TITLE, 300), UPPER(:P_SEVERITY), 'OPEN', :P_COMPANY,\n"
          "           CURRENT_TIMESTAMP(), 'UNKNOWN', :v_actor\n")
A5_OLD = ("        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED)\n"
          "    SELECT :inc_id, 'ALERT', e.EVENT_ID, e.RAISED_AT, FALSE\n")
A5_NEW = ("        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED, LINKED_BY)\n"
          "    SELECT :inc_id, 'ALERT', e.EVENT_ID, e.RAISED_AT, FALSE, :v_actor\n")

proc4 = _swap(BASE_PROC, R1_OLD, R1_NEW, "R1 0-member rollback")
proc4 = _swap(proc4, R2_OLD, R2_NEW, "R2 OK verdict")

proc5 = _swap(proc4, A1_OLD, A1_NEW, "A1 P_ACTOR")
proc5 = _swap(proc5, A2_OLD, A2_NEW, "A2 v_actor declare")
proc5 = _swap(proc5, A3_OLD, A3_NEW, "A3 v_actor assignment")
proc5 = _swap(proc5, A4_OLD, A4_NEW, "A4 DECLARED_BY")
proc5 = _swap(proc5, A5_OLD, A5_NEW, "A5 LINKED_BY")

# ---------------------------------------------------------------------------------------------------------------
# INCIDENT_PROPOSALS: P1-P6
# ---------------------------------------------------------------------------------------------------------------
P1_OLD = "                                'WH_CHANGE_REGRESSION') THEN 'WAREHOUSE'\n"
P1_NEW = ("                                'WH_CHANGE_REGRESSION', 'COST_IDLE_OPPORTUNITY',\n"
          "                                'COST_SLEEP_POLLING') THEN 'WAREHOUSE'\n")
P2_OLD = "                                'PERF_CHANGE_REGRESSION') THEN 'OBJECT'\n"
P2_NEW = ("                                'PERF_CHANGE_REGRESSION', 'PIPE_DT_FAILURES',\n"
          "                                'PIPE_VOLUME_DROP', 'DQ_BREACH', 'DQ_SCHEMA_DRIFT') THEN 'OBJECT'\n")
P3_OLD = "             WHEN r.RULE_ID IN ('SEC_CRED_EXPIRY', 'SEC_NEW_ADMIN_NETWORK') THEN 'USER'\n"
P3_NEW = ("             WHEN r.RULE_ID IN ('SEC_CRED_EXPIRY', 'SEC_NEW_ADMIN_NETWORK', 'SEC_FAILED_LOGINS',\n"
          "                                'SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT',\n"
          "                                'COST_AI_USER_RUNAWAY') THEN 'USER'\n")
P4_OLD = "               OR UPPER(r.ENTITY_RAW) IN ('DAILY', 'WARN', 'CRIT', 'MED', 'HIGH')\n"
P4_NEW = "               OR UPPER(r.ENTITY_RAW) IN ('DAILY', 'WARN', 'CRIT', 'MED', 'HIGH', 'EXH', 'ALL')\n"
P5_OLD = "         WHEN MATCHED_WH_CHANGES + MATCHED_OBJECT_CHANGES + MATCHED_TASK_FAILURES > 0 THEN 'HIGH'\n"
P5_NEW = ("         WHEN MATCHED_WH_CHANGES + MATCHED_OBJECT_CHANGES\n"
          "              + IFF(FAMILY = 'PIPE_TASK_FAILURES', 0, MATCHED_TASK_FAILURES) > 0 THEN 'HIGH'\n")
P6_OLD = "       '; matched task failures=' || MATCHED_TASK_FAILURES AS EVIDENCE\n"
P6_NEW = ("       IFF(FAMILY = 'PIPE_TASK_FAILURES', '; task failures (alert source, not corroboration)=',\n"
          "           '; matched task failures=') || MATCHED_TASK_FAILURES AS EVIDENCE\n")

view = BASE_VIEW
for _old, _new, _label in ((P1_OLD, P1_NEW, "P1 WAREHOUSE"), (P2_OLD, P2_NEW, "P2 OBJECT"), (P3_OLD, P3_NEW, "P3 USER"),
                           (P4_OLD, P4_NEW, "P4 band tokens"), (P5_OLD, P5_NEW, "P5 CONFIDENCE"),
                           (P6_OLD, P6_NEW, "P6 EVIDENCE")):
    view = _swap(view, _old, _new, _label)

# ---------------------------------------------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------------------------------------------
HEADER = f"""-- {NAME}
--
-- Incident declare + proposals (bug-hunt round 2: R2-028, R2-030, R2-093, R2-031).
--
-- WHY:
--   R2-028  The manual declare (Control Room > Incidents & triage > Proposed incidents) wrote DECLARED_BY and every
--           member's LINKED_BY from their V032 default, CURRENT_USER(). SP_INCIDENT_DECLARE is EXECUTE AS OWNER and
--           the app is owner-rights SiS, so that is the app owner for every DBA. Control Room shows the column as
--           "Declared by". A NEW 5-arg overload takes P_ACTOR (the app passes identity_sql()) and writes both columns.
--   R2-030  The proc committed an incident whose members INSERT linked 0 rows (the proposal list is up to 30 s
--           stale; a sweep, another operator or a concurrent auto-declare can empty the linkable set first) and
--           returned 'DECLARED: 0 member(s) linked', which the app never read. Nothing ever attaches to or
--           mitigates such an incident. Both overloads now roll that back and return a NOOP, and the success
--           verdict becomes 'OK: declared <id> with <n> member(s) linked'.
--   R2-093  INCIDENT_PROPOSALS (V072) knew only V072-era rules and band tokens. An EXH band (COST_CONTRACT_BREACH,
--           PIPE_ETL_CYCLE_LATE) read as an entity called EXH, so the declare guard looked only for an incident
--           holding an EXH member and opened a SECOND incident for the same late night; 'ALL' read as an entity
--           too. The user / warehouse / object rules added since V072 get their entity kind.
--   R2-031  A PIPE_TASK_FAILURES proposal counted its OWN FACT_TASK_DAILY failures as corroboration, so every one
--           read CONFIDENCE HIGH. They now reach HIGH only with a matching task change (MEDIUM on repeat days).
--
--   ~ SP_INCIDENT_DECLARE(VARCHAR x4) re-derived from V131 (its only definer): the 0-member rollback + OK verdict.
--   + SP_INCIDENT_DECLARE(VARCHAR x5) derived from V131: the same body plus P_ACTOR -> DECLARED_BY / LINKED_BY.
--     The 4-arg is KEPT so an app that is not redeployed yet still declares (it keeps crediting the owner). The
--     app CALLs the 5-arg only once has_migration(170); a later migration drops the 4-arg after that deploy.
--   ~ INCIDENT_PROPOSALS re-derived from V072 (its current definer): rule kinds, EXH/ALL band tokens, and the
--     PIPE_TASK_FAILURES confidence + evidence label. Same columns, same order (the app reads SELECT *).
--
-- COST: none at apply (two proc swaps and one view swap). The view is computed at read time on the same tables;
-- the declare proc gains one IF.
-- FIRST RUN: the next proposal read re-classifies every open proposal; the next manual declare from a 4.609.0
-- app writes the declaring DBA. Nothing runs at apply time. History is not rewritten: earlier manual declares
-- keep the app owner as Declared by (an optional, owner-run heuristic repair is staged separately).
-- ROLLBACK: re-run V131's CREATE PROCEDURE and V072's CREATE VIEW. Remove the 5-arg overload (its teardown.sql
-- line names the signature) only once no deployed app calls it: a 4.609.0 app on a V170 schema CALLs the 5-arg.
-- Apply AFTER V169. Idempotent; safe to re-run.

EXECUTE IMMEDIATE
$$
DECLARE
    v NUMBER;
    not_ready EXCEPTION (-20170, 'V170 requires V169 first - apply migrations in order.');
BEGIN
    SELECT MAX(VERSION) INTO :v FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION;
    IF (v < 169) THEN
        RAISE not_ready;
    END IF;
END;
$$;

"""

MARK4 = ("-- >>> derived:SP_INCIDENT_DECLARE  (from V131; 4-arg kept for un-redeployed apps: a member-less declare "
         "rolls back with a NOOP, success verdict OK, V170)\n")
MARK5 = ("-- >>> derived:SP_INCIDENT_DECLARE  (from V131; NEW 5-arg overload: + P_ACTOR -> DECLARED_BY / LINKED_BY, "
         "with the same rollback and OK verdict, V170)\n")
MARKV = ("-- >>> derived:INCIDENT_PROPOSALS  (from V072; EXH/ALL band tokens -> ACCOUNT, + user/warehouse/object rule "
         "kinds, PIPE_TASK_FAILURES no longer self-corroborates, V170)\n")

DESCRIPTION = (
    "Incident declare + proposals (R2-028, R2-030, R2-093, R2-031): SP_INCIDENT_DECLARE re-derived from V131 "
    "(4-arg kept) plus a NEW 5-arg overload with P_ACTOR that writes INCIDENTS.DECLARED_BY and "
    "INCIDENT_MEMBERS.LINKED_BY (the app passes the viewer once V170 is applied; CURRENT_USER() under the "
    "owner-rights app is the owner). Both overloads roll back a declare whose members INSERT linked 0 rows and "
    "return NOOP: no open alerts left to link, and the success verdict is OK: declared <id> with <n> member(s) "
    "linked. INCIDENT_PROPOSALS re-derived from V072: EXH and ALL band tokens classify ACCOUNT (an EXH band no "
    "longer opens a second incident for the same family), the user, warehouse and object rules added since V072 "
    "get their entity kind, and a PIPE_TASK_FAILURES proposal no longer counts its own task failures as "
    "corroboration (HIGH only with a matching task change; its evidence labels the count as the alert source). "
    "Same view columns. No data change, nothing runs at apply."
)
TAIL = (
    "INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION (VERSION, DESCRIPTION)\n"
    "SELECT 170 AS VERSION,\n"
    f"       '{DESCRIPTION}' AS DESCRIPTION\n"
    "WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION WHERE VERSION = 170);\n"
)

out = (HEADER + MARK4 + proc4 + "\n\n" + MARK5 + proc5 + "\n\n" + MARKV + view + "\n" + TAIL)

# post-asserts: LF only, backslash-free bodies, no inner $$, the CREATE counts, nothing runs at apply
assert "\r" not in out
assert "'" not in DESCRIPTION and len(DESCRIPTION) <= 4000
for body in (proc4, proc5):
    inner = body[body.index("$$") + 2:body.rindex("$$")]
    assert "\\" not in body and "$$" not in inner and "EXECUTE IMMEDIATE" not in inner
assert out.count("CREATE OR REPLACE PROCEDURE") == 2 and out.count("CREATE OR REPLACE VIEW") == 1
assert not re.search(r"^\s*(CALL|DROP|ALTER|GRANT|REVOKE|UPDATE|DELETE|MERGE|TRUNCATE)\b", out, re.M)
assert not re.search(r"\b(?:CREATE|ALTER|DROP|EXECUTE)\s+(?:OR\s+REPLACE\s+)?TASK\b", out) and "CALL " not in out

target = Path(os.environ.get("V170_OUT") or (MIG / NAME))
target.write_text(out, encoding="utf-8", newline="\n")
print(f"wrote {target} ({len(out):,} chars)")

# ---------------------------------------------------------------------------------------------------------------
# Optional read-only extras (never written by the byte-identity test)
# ---------------------------------------------------------------------------------------------------------------
VIEW_BODY = view[view.index(" AS\n") + len(" AS\n"):].rstrip("\n").rstrip(";")
PREFLIGHT = f"""-- PREFLIGHT V170 (read-only; run BEFORE applying V170, any role that reads DBA_MAINT_DB.OVERWATCH).
-- Every statement is a SELECT. P170.1 is the V170 INCIDENT_PROPOSALS body verbatim, run as a query, so what you
-- preview is what the view will return after the apply; compare it with P170.2 (today's view).

-- P170.1  open proposals as V170 will classify and score them (ENTITY_KIND / CONFIDENCE / EVIDENCE).
{VIEW_BODY}
ORDER BY ENTITY_KIND, CONFIDENCE, PROPOSAL_KEY;

-- P170.2  the same proposals as the live (V072) view returns them today; EXH / ALL rows read ENTITY_KIND SCOPE.
SELECT PROPOSAL_KEY, ENTITY_KIND, ENTITY_NAME, CONFIDENCE, ALERTS, EVIDENCE
FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS
ORDER BY ENTITY_KIND, CONFIDENCE, PROPOSAL_KEY;

-- P170.3  SP_INCIDENT_DECLARE overloads before the apply: expect ONE row (the V131 4-arg); V170 adds the 5-arg.
SELECT PROCEDURE_NAME, ARGUMENT_SIGNATURE, CREATED, LAST_ALTERED
FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
WHERE PROCEDURE_SCHEMA = 'OVERWATCH' AND PROCEDURE_NAME = 'SP_INCIDENT_DECLARE'
ORDER BY ARGUMENT_SIGNATURE;
"""

PART_B = """-- RUN_NEXT PART B -- V170 verify (read-only; every statement is a SELECT).

-- V170.1  both SP_INCIDENT_DECLARE overloads exist: expect TWO rows, the 4-arg and the 5-arg (P_ACTOR VARCHAR).
SELECT PROCEDURE_NAME, ARGUMENT_SIGNATURE, LAST_ALTERED
FROM DBA_MAINT_DB.INFORMATION_SCHEMA.PROCEDURES
WHERE PROCEDURE_SCHEMA = 'OVERWATCH' AND PROCEDURE_NAME = 'SP_INCIDENT_DECLARE'
ORDER BY ARGUMENT_SIGNATURE;

-- V170.2  the view carries the V170 text: expect BAND_TOKENS_OK, USER_KINDS_OK and TASK_SELF_EVIDENCE_OK all TRUE.
SELECT CONTAINS(d, '''DAILY'', ''WARN'', ''CRIT'', ''MED'', ''HIGH'', ''EXH'', ''ALL''') AS BAND_TOKENS_OK,
       CONTAINS(d, '''SEC_LOGIN_TAKEOVER'', ''SEC_ADMIN_GRANT''') AS USER_KINDS_OK,
       CONTAINS(d, 'IFF(FAMILY = ''PIPE_TASK_FAILURES'', 0, MATCHED_TASK_FAILURES)') AS TASK_SELF_EVIDENCE_OK
FROM (SELECT GET_DDL('VIEW', 'DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS') AS d);

-- V170.3  open proposals by kind and confidence: no ENTITY_NAME of EXH or ALL is left, and PIPE_TASK_FAILURES rows
-- are LOW or MEDIUM unless a task change matched (MATCHED_OBJECT_CHANGES > 0).
SELECT ENTITY_KIND, CONFIDENCE, COUNT(*) AS PROPOSALS,
       COUNT_IF(UPPER(ENTITY_NAME) IN ('EXH', 'ALL')) AS BAND_AS_ENTITY,
       COUNT_IF(SPLIT_PART(PROPOSAL_KEY, '|', 1) = 'PIPE_TASK_FAILURES') AS TASK_FAILURE_PROPOSALS
FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS
GROUP BY 1, 2
ORDER BY 1, 2;

-- V170.4  (R2-030 owner list) OPEN / MITIGATED incidents with NO member: each one is a past empty declare (or the
-- auto-declare race); close each from Control Room > Incidents with its root cause. Expect no NEW rows after V170.
SELECT i.INCIDENT_ID, i.TITLE, i.COMPANY, i.STATUS, i.DETECTED_AT, i.DECLARED_BY
FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
WHERE i.STATUS IN ('OPEN', 'MITIGATED')
  AND NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m WHERE m.INCIDENT_ID = i.INCIDENT_ID)
ORDER BY i.DETECTED_AT;

-- V170.5  (R2-093 owner list) open incidents holding an EXH-band member alert: compare each with any other open
-- incident of the same family (FAMILY_OPEN_INCIDENTS > 1 is the duplicate the old view allowed).
SELECT i.INCIDENT_ID, i.STATUS, i.DETECTED_AT, a.RULE_ID, a.DEDUPE_KEY,
       (SELECT COUNT(DISTINCT i2.INCIDENT_ID)
        FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i2
        JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m2 ON m2.INCIDENT_ID = i2.INCIDENT_ID AND m2.MEMBER_KIND = 'ALERT'
        JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a2 ON a2.EVENT_ID = m2.REF_ID
        WHERE i2.STATUS IN ('OPEN', 'MITIGATED')
          AND SPLIT_PART(COALESCE(a2.DEDUPE_KEY, a2.EVENT_ID), '|', 1) = a.RULE_ID) AS FAMILY_OPEN_INCIDENTS
FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m ON m.INCIDENT_ID = i.INCIDENT_ID AND m.MEMBER_KIND = 'ALERT'
JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS a ON a.EVENT_ID = m.REF_ID
WHERE SPLIT_PART(a.DEDUPE_KEY, '|', 2) = 'EXH'
  AND i.STATUS IN ('OPEN', 'MITIGATED')
ORDER BY i.DETECTED_AT;

-- V170.6  after the first manual declare from the 4.609.0 app: the newest manual incidents and their members name
-- the declaring DBA (not the app owner). Auto-declared rows keep SP_INCIDENT_AUTODECLARE.
SELECT i.INCIDENT_ID, i.DETECTED_AT, i.DECLARED_BY,
       LISTAGG(DISTINCT m.LINKED_BY, ', ') AS LINKED_BY, COUNT(m.REF_ID) AS MEMBERS
FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS i
LEFT JOIN DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS m ON m.INCIDENT_ID = i.INCIDENT_ID
WHERE i.DECLARED_BY <> 'SP_INCIDENT_AUTODECLARE'
  AND i.DETECTED_AT >= DATEADD('day', -14, CURRENT_TIMESTAMP())
GROUP BY 1, 2, 3
ORDER BY i.DETECTED_AT DESC
LIMIT 20;
"""

for _env, _text in (("PREFLIGHT_OUT", PREFLIGHT), ("PART_B_OUT", PART_B)):
    _dest = os.environ.get(_env)
    if _dest:
        assert "\r" not in _text and "$$" not in _text
        Path(_dest).write_text(_text, encoding="utf-8", newline="\n")
        print(f"wrote {_dest} ({len(_text):,} chars)")
