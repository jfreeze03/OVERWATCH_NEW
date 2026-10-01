"""Locks for V170 -- the manual incident declare credits the DBA (R2-028) and never commits an empty incident
(R2-030); INCIDENT_PROPOSALS classifies today's rules and band tokens (R2-093) and stops a task-failure proposal
self-corroborating (R2-031).

STRUCTURE + GENERATION + LOCKS; tests/migrations/test_v170_harness.py EXECUTES the view and the declare statements.
What this file proves:
  * generation -- outputs/gen_v170.py regenerates the migration byte-for-byte, reads only V131 + V072, never imports
    app/; the read-only PREFLIGHT / PART B are written only on request, parse, and the PREFLIGHT carries the view
    body verbatim;
  * shape -- first line, guard (-20170, v < 169), marker + 4-arg proc, marker + 5-arg proc, marker + view, version
    row; nothing runs at apply time; no DROP of the kept 4-arg overload; no DEFAULT on P_ACTOR;
  * lineage + round 13 -- every object names its immediately previous definer, and reversing the declared deltas
    (with this file's OWN copies of the old and new text) gives V131's proc and V072's view back byte-for-byte;
  * the classification of every alert rule's dedupe key, and the confidence / evidence change;
  * the app half (control_room): the verdict classifier, the gated actor CALL, execute_action, the receipts.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._source import ROOT, read
from tests.test_migration_proc_syntax import _strip_noise

_NAME = "V170__incident_declare_actor_and_proposals.sql"
_MIG = read(f"snowflake/migrations/{_NAME}")
_V131 = read("snowflake/migrations/V131__incident_declare_atomic.sql")
_V072 = read("snowflake/migrations/V072__entity_aware_incident_proposals.sql")
_CR = read("app/ui/pages/control_room.py")

_CREATE_PROC = "CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE("
_CREATE_VIEW = "CREATE OR REPLACE VIEW DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS AS\n"


def _procs(text: str) -> list[str]:
    out, at = [], 0
    while (s := text.find(_CREATE_PROC, at)) >= 0:
        o = text.index("$$", s)
        e = text.index("$$;", o + 2) + 3
        out.append(text[s:e])
        at = e
    return out


def _view(text: str) -> str:
    s = text.index(_CREATE_VIEW)
    return text[s:text.index("\nFROM evidence;\n", s) + len("\nFROM evidence;\n")]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


(_P131,) = _procs(_V131)
_P4, _P5 = _procs(_MIG)
_VIEW = _view(_MIG)
_VIEW72 = _view(_V072)

# ---------------------------------------------------------------------------------------------------
# Test-side copies of every declared delta (independent of outputs/gen_v170.py).
# ---------------------------------------------------------------------------------------------------
_MARK4 = ("-- >>> derived:SP_INCIDENT_DECLARE  (from V131; 4-arg kept for un-redeployed apps: a member-less declare "
          "rolls back with a NOOP, success verdict OK, V170)\n")
_MARK5 = ("-- >>> derived:SP_INCIDENT_DECLARE  (from V131; NEW 5-arg overload: + P_ACTOR -> DECLARED_BY / LINKED_BY, "
          "with the same rollback and OK verdict, V170)\n")
_MARKV = ("-- >>> derived:INCIDENT_PROPOSALS  (from V072; EXH/ALL band tokens -> ACCOUNT, + user/warehouse/object rule "
          "kinds, PIPE_TASK_FAILURES no longer self-corroborates, V170)\n")

_ROLLBACK_BLOCK = (
    "    -- V170 (R2-030): never commit a member-less incident. The proposal the operator declared from can be up\n"
    "    -- to 30 s stale, and an auto-clear or condition-ended sweep, another operator resolving the alerts, or a\n"
    "    -- concurrent auto-declare can take every proposal alert out of OPEN/ACK (or into another incident)\n"
    "    -- before this CALL; the members INSERT then links 0 rows. Undo the incident row (it was never visible\n"
    "    -- outside this transaction) and say so.\n"
    "    IF (:created = 1 AND :members = 0) THEN\n"
    "        ROLLBACK;\n"
    "        RETURN 'NOOP: no open alerts left to link - nothing declared';\n"
    "    END IF;\n"
    "\n")
_RET_OLD = "    RETURN 'DECLARED: ' || :members || ' member(s) linked';\n"
_RET_NEW = "    RETURN 'OK: declared ' || :inc_id || ' with ' || :members || ' member(s) linked';\n"
_FAMILY_OPEN_RET = "        RETURN 'NOOP: this family already has an open incident';\n"
_ACTOR_EXPR = "LEFT(COALESCE(NULLIF(TRIM(:P_ACTOR), ''), CURRENT_USER()), 200)"
_ACTOR_DELTAS = (   # (V170 5-arg text, the 4-arg text it reverses to)
    ("    P_PROPOSAL_KEY VARCHAR,\n    P_ACTOR VARCHAR\n)\n", "    P_PROPOSAL_KEY VARCHAR\n)\n"),
    ("    inc_id STRING;\n    v_actor STRING;\n", "    inc_id STRING;\n"),
    ("    inc_id := UUID_STRING();\n"
     "    -- V170 (R2-028): the DBA who declared. The app passes identity_sql() (st.user under owner-rights SiS);\n"
     "    -- CURRENT_USER() there is the app owner, so it is only the fallback. LEFT(..., 200) = the width of\n"
     "    -- INCIDENTS.DECLARED_BY and INCIDENT_MEMBERS.LINKED_BY (V032).\n"
     f"    v_actor := {_ACTOR_EXPR};\n", "    inc_id := UUID_STRING();\n"),
    ("        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, ROOT_CAUSE_KIND, DECLARED_BY)\n"
     "    SELECT :inc_id, LEFT(:P_TITLE, 300), UPPER(:P_SEVERITY), 'OPEN', :P_COMPANY,\n"
     "           CURRENT_TIMESTAMP(), 'UNKNOWN', :v_actor\n",
     "        (INCIDENT_ID, TITLE, SEVERITY, STATUS, COMPANY, DETECTED_AT, ROOT_CAUSE_KIND)\n"
     "    SELECT :inc_id, LEFT(:P_TITLE, 300), UPPER(:P_SEVERITY), 'OPEN', :P_COMPANY,\n"
     "           CURRENT_TIMESTAMP(), 'UNKNOWN'\n"),
    ("        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED, LINKED_BY)\n"
     "    SELECT :inc_id, 'ALERT', e.EVENT_ID, e.RAISED_AT, FALSE, :v_actor\n",
     "        (INCIDENT_ID, MEMBER_KIND, REF_ID, EVIDENCE_TS, AUTO_LINKED)\n"
     "    SELECT :inc_id, 'ALERT', e.EVENT_ID, e.RAISED_AT, FALSE\n"),
)
_VIEW_DELTAS = (    # (V170 text, V072 text)
    ("                                'WH_CHANGE_REGRESSION', 'COST_IDLE_OPPORTUNITY',\n"
     "                                'COST_SLEEP_POLLING') THEN 'WAREHOUSE'\n",
     "                                'WH_CHANGE_REGRESSION') THEN 'WAREHOUSE'\n"),
    ("                                'PERF_CHANGE_REGRESSION', 'PIPE_DT_FAILURES',\n"
     "                                'PIPE_VOLUME_DROP', 'DQ_BREACH', 'DQ_SCHEMA_DRIFT') THEN 'OBJECT'\n",
     "                                'PERF_CHANGE_REGRESSION') THEN 'OBJECT'\n"),
    ("             WHEN r.RULE_ID IN ('SEC_CRED_EXPIRY', 'SEC_NEW_ADMIN_NETWORK', 'SEC_FAILED_LOGINS',\n"
     "                                'SEC_LOGIN_TAKEOVER', 'SEC_ADMIN_GRANT',\n"
     "                                'COST_AI_USER_RUNAWAY') THEN 'USER'\n",
     "             WHEN r.RULE_ID IN ('SEC_CRED_EXPIRY', 'SEC_NEW_ADMIN_NETWORK') THEN 'USER'\n"),
    ("               OR UPPER(r.ENTITY_RAW) IN ('DAILY', 'WARN', 'CRIT', 'MED', 'HIGH', 'EXH', 'ALL')\n",
     "               OR UPPER(r.ENTITY_RAW) IN ('DAILY', 'WARN', 'CRIT', 'MED', 'HIGH')\n"),
    ("         WHEN MATCHED_WH_CHANGES + MATCHED_OBJECT_CHANGES\n"
     "              + IFF(FAMILY = 'PIPE_TASK_FAILURES', 0, MATCHED_TASK_FAILURES) > 0 THEN 'HIGH'\n",
     "         WHEN MATCHED_WH_CHANGES + MATCHED_OBJECT_CHANGES + MATCHED_TASK_FAILURES > 0 THEN 'HIGH'\n"),
    ("       IFF(FAMILY = 'PIPE_TASK_FAILURES', '; task failures (alert source, not corroboration)=',\n"
     "           '; matched task failures=') || MATCHED_TASK_FAILURES AS EVIDENCE\n",
     "       '; matched task failures=' || MATCHED_TASK_FAILURES AS EVIDENCE\n"),
)


def _declare_back_to_v131(proc: str) -> str:
    assert proc.count(_ROLLBACK_BLOCK) == 1 and proc.count(_RET_NEW) == 1
    return proc.replace(_ROLLBACK_BLOCK, "").replace(_RET_NEW, _RET_OLD)


def _actor_back(proc: str) -> str:
    for new, old in _ACTOR_DELTAS:
        assert proc.count(new) == 1, new
        proc = proc.replace(new, old)
    return proc


def _view_back(view: str) -> str:
    for new, old in _VIEW_DELTAS:
        assert view.count(new) == 1, new
        view = view.replace(new, old)
    return view


# -- generation ----------------------------------------------------------------------------------------------

def _gen_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("V170_OUT", "PREFLIGHT_OUT", "PART_B_OUT")}
    env.update(extra)
    return env


def _run_gen(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v170.py")], env=_gen_env(**extra),
                          cwd=tmp_path, capture_output=True, text=True)


def _extras(tmp_path: Path) -> tuple[str, str]:
    pre, part_b = tmp_path / "PREFLIGHT_V170.sql", tmp_path / "PART_B_V170.sql"
    result = _run_gen(tmp_path, V170_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(pre), PART_B_OUT=str(part_b))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "m.sql").read_text(encoding="utf-8") == _MIG      # the extras never change the migration
    return pre.read_text(encoding="utf-8"), part_b.read_text(encoding="utf-8")


def test_v170_regenerates_byte_identical_and_writes_nothing_else(tmp_path):
    out = tmp_path / "regen.sql"
    result = _run_gen(tmp_path, V170_OUT=str(out))
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == (ROOT / "snowflake" / "migrations" / _NAME).read_bytes(), (
        "V170 drifted from its forward-generation -- edit outputs/gen_v170.py, not the .sql.")
    assert b"\r\n" not in out.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["regen.sql"]     # PREFLIGHT / PART B only on request


def test_v170_generator_reads_only_its_bases_and_never_imports_app():
    gen = read("outputs/gen_v170.py")
    assert re.findall(r'MIG / "(V\d+__\w+\.sql)"', gen) == ["V131__incident_declare_atomic.sql",
                                                           "V072__entity_aware_incident_proposals.sql"]
    assert "import app" not in gen and "from app" not in gen


@pytest.mark.parametrize("which", ["preflight", "part_b"])
def test_v170_preflight_and_part_b_are_read_only_and_parse(tmp_path, which):
    pre, part_b = _extras(tmp_path)
    sql = pre if which == "preflight" else part_b
    code = _strip_noise(sql)                       # comments + strings out
    for banned in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "CREATE", "ALTER", "DROP", "TRUNCATE",
                   "GRANT", "REVOKE", "EXECUTE"):
        assert not re.search(rf"\b{banned}\b", code, re.I), (which, banned)
    assert not re.search(r"(?<![:\w]):[A-Za-z_]\w*", code), "a scripting :bind survived"
    assert "$$" not in sql and "@" not in sql
    sqlglot = pytest.importorskip("sqlglot")
    from sqlglot import exp
    parsed = [p for p in sqlglot.parse(sql, dialect="snowflake") if p is not None]
    assert len(parsed) == (3 if which == "preflight" else 6)
    writes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Command)
    for tree in parsed:
        assert tree.key == "select", tree.key
        assert not [type(n).__name__ for n in tree.walk() if isinstance(n, writes)]
    grids = ("-- P170.1 ", "-- P170.2 ", "-- P170.3 ") if which == "preflight" else tuple(
        f"-- V170.{i} " for i in range(1, 7))
    for grid in grids:
        assert sql.count(grid) == 1, grid


def test_v170_preflight_carries_the_view_body_verbatim(tmp_path):
    """What the owner previews is what the view returns after the apply."""
    pre, _ = _extras(tmp_path)
    body = _VIEW[len(_CREATE_VIEW):].rstrip("\n").rstrip(";")
    assert pre.count(body + "\nORDER BY ENTITY_KIND, CONFIDENCE, PROPOSAL_KEY;\n") == 1


# -- guard, order, shape -------------------------------------------------------------------------------------

def test_v170_first_line_guard_and_version():
    assert _MIG.startswith(f"-- {_NAME}\n")
    assert "not_ready EXCEPTION (-20170, 'V170 requires V169 first - apply migrations in order.');" in _MIG
    assert "IF (v < 169) THEN" in _MIG
    assert "SELECT 170 AS VERSION" in _MIG and "WHERE VERSION = 170);" in _MIG
    guard131 = _between(_V131, "EXECUTE IMMEDIATE\n$$\n", "$$;\n")          # V131's guard, numbers moved
    want = (guard131.replace("-20131", "-20170").replace("'V131 requires V130 first", "'V170 requires V169 first")
            .replace("IF (v < 130)", "IF (v < 169)"))
    assert _between(_MIG, "EXECUTE IMMEDIATE\n$$\n", "$$;\n") == want
    assert _MIG.count("EXECUTE IMMEDIATE") == 1
    assert _MIG.rstrip().endswith("WHERE NOT EXISTS (SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION "
                                  "WHERE VERSION = 170);")
    header = _MIG[:_MIG.index("EXECUTE IMMEDIATE")]
    for word in ("WHY:", "COST:", "FIRST RUN:", "ROLLBACK:", "Apply AFTER V169. Idempotent; safe to re-run.",
                 "R2-028", "R2-030", "R2-093", "R2-031"):
        assert word in header, word


def test_v170_file_order_and_statements():
    from tests.test_migrations_parse import _plain_statements
    guard = _MIG.index("EXCEPTION (-20170")
    m4, p4 = _MIG.index(_MARK4), _MIG.index(_P4)
    m5, p5 = _MIG.index(_MARK5), _MIG.index(_P5)
    mv, v = _MIG.index(_MARKV), _MIG.index(_VIEW)
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < m4 < p4 < m5 < p5 < mv < v < version
    for mark, obj in ((m4, _MARK4), (m5, _MARK5), (mv, _MARKV)):        # each marker sits directly above its CREATE
        assert _MIG[mark + len(obj):].startswith("CREATE OR REPLACE ")
    kinds = [re.sub(r"^(?:--[^\n]*\n)+", "", s.strip()).split(None, 4)[:4] for s in _plain_statements(_MIG)]
    assert kinds == [["CREATE", "OR", "REPLACE", "VIEW"],
                     ["INSERT", "INTO", "DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION", "(VERSION,"]], kinds
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 2 and _MIG.count("CREATE OR REPLACE VIEW") == 1
    assert _MIG.count("$$") == 6                                     # guard + two proc bodies
    top = _strip_noise("".join(p for i, p in enumerate(_MIG.split("$$")) if i % 2 == 0))
    for banned in ("CALL", "ALTER", "DROP", "TASK", "GRANT", "REVOKE", "TRUNCATE", "UPDATE", "DELETE", "MERGE",
                   "FUNCTION", "TABLE", "COPY"):
        assert not re.search(rf"\b{banned}\b", top), banned
    assert not re.search(r"\$[A-Za-z_][A-Za-z0-9_]*\$", _MIG)
    assert "\r" not in _MIG


def test_v170_keeps_the_4_arg_overload_and_adds_no_default():
    """No DROP of the V131 overload (an un-redeployed app still CALLs it) and no DEFAULT on P_ACTOR (Snowflake
    rejects an optional-argument overload that is ambiguous with the kept 4-arg one)."""
    assert "DROP PROCEDURE" not in _MIG
    sig4 = _P4[:_P4.index(")\nRETURNS VARCHAR")]
    sig5 = _P5[:_P5.index(")\nRETURNS VARCHAR")]
    assert sig4.count(" VARCHAR") == 4 and sig5.count(" VARCHAR") == 5
    assert "DEFAULT" not in sig5 and "P_ACTOR VARCHAR" in sig5 and "P_ACTOR" not in _P4
    assert "v_actor" not in _P4 and "DECLARED_BY" not in _P4 and "LINKED_BY" not in _P4


def test_v170_description_fits_and_has_no_apostrophe():
    desc = re.search(r"SELECT 170 AS VERSION,\n       '(.*)' AS DESCRIPTION\n", _MIG, re.S).group(1)
    assert "\n" not in desc and "'" not in desc
    assert len(desc) <= 4000
    for term in ("re-derived from V131", "re-derived from V072", "5-arg", "NOOP: no open alerts left to link"):
        assert term in desc, term


# -- lineage + round 13 ------------------------------------------------------------------------------------

def test_v170_markers_name_the_current_definers():
    from tests.test_proc_lineage import _HISTORICAL_WAIVERS, _definers, _lineage, _migrations, _violations
    texts = _migrations()
    rows = {(r["v"], r["proc"]): r for r in _lineage(texts)}
    d = rows[(170, "SP_INCIDENT_DECLARE")]
    assert d["src"] == "marker" and d["claims"] == [131] and d["prev"] == 131 and not d["waived"], d
    p = rows[(170, "INCIDENT_PROPOSALS")]
    assert p["src"] == "marker" and p["claims"] == [72] and p["prev"] == 72 and not p["waived"], p
    defs = _definers(texts)
    assert [x for x in defs["SP_INCIDENT_DECLARE"] if x <= 170] == [131, 170]
    assert [x for x in defs["INCIDENT_PROPOSALS"] if x <= 170] == [32, 72, 170]
    assert not [x for x in _violations(texts, _HISTORICAL_WAIVERS) if x.startswith("V170 ")]
    assert "LINEAGE-WAIVER" not in _MIG and _MIG.count("-- >>> derived:") == 3


def test_v170_4_arg_normalizes_back_to_v131_byte_for_byte():
    assert _declare_back_to_v131(_P4) == _P131


def test_v170_5_arg_differs_from_the_4_arg_only_in_attribution():
    assert _actor_back(_P5) == _P4
    assert _declare_back_to_v131(_actor_back(_P5)) == _P131


def test_v170_view_normalizes_back_to_v072_byte_for_byte():
    assert _view_back(_VIEW) == _VIEW72


@pytest.mark.parametrize("victim", ["'NOOP: this family already has an open incident'", "NOT :apply_entity",
                                    "DATEADD('day', -2, CURRENT_TIMESTAMP())", "WHERE i2.INCIDENT_ID = :inc_id"])
def test_v170_proc_normalize_has_teeth(victim):
    """One character dropped outside the declared deltas breaks the byte compare (both overloads)."""
    for proc, back in ((_P4, _declare_back_to_v131), (_P5, lambda p: _declare_back_to_v131(_actor_back(p)))):
        assert proc.count(victim) >= 1
        assert back(proc.replace(victim, victim[:-1], 1)) != _P131, victim


@pytest.mark.parametrize("victim", ["'MEDIUM') AS SEVERITY", "GROUP BY FAMILY, COMPANY, ENTITY_KIND, ENTITY_NAME",
                                    "WHEN ENTITY_KIND <> 'ACCOUNT' AND ALERTS >= 2 THEN 'MEDIUM'",
                                    "DATEADD('day', 1, DATE(g.LAST_TS))"])
def test_v170_view_normalize_has_teeth(victim):
    assert _VIEW.count(victim) == 1
    assert _view_back(_VIEW.replace(victim, victim[:-1], 1)) != _VIEW72, victim


# -- the declare deltas ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("proc", ["4", "5"])
def test_v170_rollback_sits_between_the_counts_and_the_commit(proc):
    p = _P4 if proc == "4" else _P5
    members = p.index("    SELECT COUNT(*) INTO :members FROM DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS")
    created = p.index("    SELECT COUNT(*) INTO :created FROM DBA_MAINT_DB.OVERWATCH.INCIDENTS")
    rb = p.index("    IF (:created = 1 AND :members = 0) THEN\n        ROLLBACK;\n"
                 "        RETURN 'NOOP: no open alerts left to link - nothing declared';\n    END IF;\n")
    commit = p.index("    COMMIT;\n")
    assert created < members < rb < commit
    assert p.count("    COMMIT;\n") == 1 and p.count("BEGIN TRANSACTION;") == 1
    assert p.count(_FAMILY_OPEN_RET) == 1 and p.index(_FAMILY_OPEN_RET) > commit
    assert p.count(_RET_NEW) == 1 and "'DECLARED: '" not in p
    assert p.index(_RET_NEW) > p.index(_FAMILY_OPEN_RET)
    body = p[p.index("$$") + 2:p.rindex("$$")]
    assert "\\" not in p and "EXECUTE IMMEDIATE" not in body and "$$" not in body
    # the verdicts the app classifies (control_room._declare_verdict): OK / NOOP-family / NOOP-empty / INVALID
    assert re.findall(r"RETURN '([A-Z]+):", body) == ["INVALID", "NOOP", "NOOP", "OK"]


def test_v170_actor_is_written_to_both_columns_from_one_bounded_expression():
    assert _P5.count(f"    v_actor := {_ACTOR_EXPR};\n") == 1
    assert _P5.count(":v_actor") == 2
    ins = _between(_P5, "    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS\n", "    WHERE NOT EXISTS (")
    assert ins.endswith("'UNKNOWN', :v_actor\n") and ", DECLARED_BY)" in ins
    mem = _between(_P5, "    INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS\n", "    FROM DBA_MAINT_DB")
    assert mem.endswith("FALSE, :v_actor\n") and ", LINKED_BY)" in mem
    # the assignment follows the key check and the UUID, before anything reads it
    assert _P5.index("RETURN 'INVALID:") < _P5.index("v_actor :=") < _P5.index("BEGIN TRANSACTION;")
    # V032 widths: DECLARED_BY / LINKED_BY VARCHAR(200)
    v032 = read("snowflake/migrations/V032__incident_object.sql")
    assert "DECLARED_BY     VARCHAR(200)" in v032 and "LINKED_BY    VARCHAR(200)" in v032


# -- the view: classification + confidence ---------------------------------------------------------------------

_CASE = _between(_VIEW, "           CASE\n", "           END AS ENTITY_KIND")


def _arms() -> list[tuple[set[str], str]]:
    out = []
    for m in re.finditer(r"WHEN r\.RULE_ID (?:IN \(([^)]*)\)|= '(\w+)') THEN '(\w+)'", _CASE):
        rules = set(re.findall(r"'(\w+)'", m.group(1))) if m.group(1) else {m.group(2)}
        out.append((rules, m.group(3)))
    return out


def _bands() -> set[str]:
    (inlist,) = re.findall(r"OR UPPER\(r\.ENTITY_RAW\) IN \(([^)]*)\)", _CASE)
    return set(re.findall(r"'(\w+)'", inlist))


def _kind(key: str) -> str:
    """The V170 CASE, evaluated in Python from its parsed arms (the harness runs the real SQL)."""
    rule, part2 = key.split("|")[0], [*key.split("|"), ""][1]
    for rules, kind in _arms():
        if rule in rules:
            return kind
    if part2 == "" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", part2) or part2.upper() in _bands():
        return "ACCOUNT"
    return "SCOPE"


# One representative DEDUPE_KEY per rule, in the shape its CURRENT producer writes (RULE_ID = family = part 1):
# SP_ALERT_SCAN (V162), SP_ALERT_SCAN_DAILY (V163), SP_SCAN_ETL_CYCLE (V156), SP_ANOMALY_SWEEP / SP_SCAN_CLOUD_SVC_ANOMALY
# (V150), SP_SCAN_SCHEMA_DRIFT (V133), SP_SCAN_SLEEP_POLLING (V160), SP_WAREHOUSE_CHANGE_SCAN (V109),
# SP_CHANGE_IMPACT_SCAN (V140), SP_SLO_BREACH_SCAN, SP_CANARY_SENTINEL.
EXPECTED: dict[str, tuple[str, str]] = {
    "COST_AI_CREEP": ("COST_AI_CREEP|2026-09-28", "ACCOUNT"),
    "COST_AI_USER_RUNAWAY": ("COST_AI_USER_RUNAWAY|JDOE|2026-09-30", "USER"),
    "COST_ANOMALY_SWEEP": ("COST_ANOMALY_SWEEP|WAREHOUSE WH_ALFA_ETL|2026-09-30", "SCOPE"),
    "COST_BUDGET_PACE": ("COST_BUDGET_PACE|ALL|2026-10-01", "ACCOUNT"),
    "COST_CLOUD_SVC_ANOMALY": ("COST_CLOUD_SVC_ANOMALY|CLOUD SVC WH_ALFA_ETL|2026-09-30", "SCOPE"),
    "COST_CONTRACT_BREACH": ("COST_CONTRACT_BREACH|EXH|2026-09-28", "ACCOUNT"),
    "COST_DAILY_CREDITS": ("COST_DAILY_CREDITS|ALL|2026-09-30", "ACCOUNT"),
    "COST_DEPT_BUDGET_PACE": ("COST_DEPT_BUDGET_PACE|FINANCE|HIGH|2026-10-01", "DEPARTMENT"),
    "COST_EGRESS_SPIKE": ("COST_EGRESS_SPIKE|2026-10-01", "ACCOUNT"),
    "COST_FORECAST_BREACH": ("COST_FORECAST_BREACH|ALL|2026-10-01", "ACCOUNT"),
    "COST_IDLE_OPPORTUNITY": ("COST_IDLE_OPPORTUNITY|WH_ALFA_ETL|HIGH|2026-09-28", "WAREHOUSE"),
    "COST_ORG_ACCOUNT_CREEP": ("COST_ORG_ACCOUNT_CREEP|ALFA_PROD|2026-09-28", "SCOPE"),
    "COST_SERVERLESS_CREEP": ("COST_SERVERLESS_CREEP|SEARCH_OPTIMIZATION|2026-09-28", "SERVICE"),
    "COST_SLEEP_POLLING": ("COST_SLEEP_POLLING|WH_TRXS_TRANSFORM|abc123|MED|2026-09-28", "WAREHOUSE"),
    "COST_STORAGE_SURGE": ("COST_STORAGE_SURGE|ALFA_EDW|2026-09-30", "DATABASE"),
    "COST_WH_DAILY_CREDITS": ("COST_WH_DAILY_CREDITS|WH_ALFA_ETL|2026-09-30", "WAREHOUSE"),
    "DQ_BREACH": ("DQ_BREACH|ALFA_EDW.CORE.POLICY|2026-09-30", "OBJECT"),
    "DQ_RECON_ERROR": ("DQ_RECON_ERROR|2026-10-01", "ACCOUNT"),
    "DQ_SCHEMA_DRIFT": ("DQ_SCHEMA_DRIFT|ALFA_EDW.CORE.POLICY|2026-10-01", "OBJECT"),
    "OPS_CANARY_FAIL": ("OPS_CANARY_FAIL|2026-10-01", "ACCOUNT"),
    "OPS_PIPELINE_DEGRADED": ("OPS_PIPELINE_DEGRADED|STALE|FACT_TASK_DAILY|2026-09-29", "SCOPE"),
    "OPS_SCAN_DEGRADED": ("OPS_SCAN_DEGRADED|2026-10-01", "ACCOUNT"),
    "OPS_SLOW_RENDER": ("OPS_SLOW_RENDER|Overview|2026-09-28", "SCOPE"),
    "PERF_CHANGE_REGRESSION": ("PERF_CHANGE_REGRESSION|ALFA_EDW.CORE.T_LOAD|2026-09-30", "OBJECT"),
    "PERF_FINGERPRINT_DRIFT": ("PERF_FINGERPRINT_DRIFT|9f86d081884c7d65|2026-09-30", "SCOPE"),
    "PERF_QUERY_FAIL_PCT": ("PERF_QUERY_FAIL_PCT|ALFA|2026-10-01", "SCOPE"),
    "PERF_QUEUED_MINUTES": ("PERF_QUEUED_MINUTES|WH_ALFA_ETL|2026-10-01", "WAREHOUSE"),
    "PERF_SLO_BREACH": ("PERF_SLO_BREACH|slo-7|CRIT|2026-10-01", "SCOPE"),
    "PERF_SPILL_GB": ("PERF_SPILL_GB|WH_ALFA_ETL|2026-10-01", "WAREHOUSE"),
    "PIPE_COPY_FAILURES": ("PIPE_COPY_FAILURES|ALFA_RAW.LAND.POLICY|CRIT|2026-10-01", "OBJECT"),
    "PIPE_DT_FAILURES": ("PIPE_DT_FAILURES|ALFA_EDW.CORE.DT_POLICY|2026-10-01", "OBJECT"),
    "PIPE_ETL_CYCLE_LATE": ("PIPE_ETL_CYCLE_LATE|EXH|2026-09-30", "ACCOUNT"),
    "PIPE_ETL_CYCLE_NOT_STARTED": ("PIPE_ETL_CYCLE_NOT_STARTED|2026-10-07", "ACCOUNT"),
    "PIPE_ETL_TASK_FAILED": ("PIPE_ETL_TASK_FAILED|WF_NIGHTLY_LOAD|2026-09-30", "SCOPE"),
    "PIPE_REF_GAP": ("PIPE_REF_GAP|policy_vs_claim|2026-10-01", "SCOPE"),
    "PIPE_TASK_FAILURES": ("PIPE_TASK_FAILURES|ALFA_EDW.CORE.T_LOAD|2026-09-30", "OBJECT"),
    "PIPE_VOLUME_DROP": ("PIPE_VOLUME_DROP|ALFA_EDW.CORE.POLICY|2026-10-01", "OBJECT"),
    "SEC_ADMIN_GRANT": ("SEC_ADMIN_GRANT|JDOE|ACCOUNTADMIN|2026-10-01 08:00:00.000", "USER"),
    "SEC_CRED_EXPIRY": ("SEC_CRED_EXPIRY|SVC_LOADER|key1|EXPIRING", "USER"),
    "SEC_FAILED_LOGINS": ("SEC_FAILED_LOGINS|JDOE|2026-09-30", "USER"),
    "SEC_LOGIN_TAKEOVER": ("SEC_LOGIN_TAKEOVER|JDOE|CRIT|2026-10-01 02:14:09.123", "USER"),
    "SEC_NEW_ADMIN_NETWORK": ("SEC_NEW_ADMIN_NETWORK|JDOE|10.1.2.3", "USER"),
    "SEC_NEW_EXPOSURE": ("SEC_NEW_EXPOSURE|USAGE|DATABASE|2026-10-01 07:00:00", "SCOPE"),
    "SEC_POSTURE_METRIC": ("SEC_POSTURE_METRIC|ALFA|2026-10-01", "SCOPE"),
    "SEC_TRUST_REGRESSION": ("SEC_TRUST_REGRESSION|CIS_BENCHMARKS|2026-09-30", "SCOPE"),
    "WH_CHANGE_REGRESSION": ("WH_CHANGE_REGRESSION|WH_ALFA_ETL|WAREHOUSE_SIZE|2026-09-30", "WAREHOUSE"),
}


def test_v170_every_rule_has_a_classification_decision():
    """A new alert rule must be classified on purpose: every live ALERT_CONFIG rule is a key of EXPECTED."""
    from tests.test_alert_rule_consistency import _config_deleted, _config_seeded_ever
    live = _config_seeded_ever() - _config_deleted()
    assert live and not live - set(EXPECTED), sorted(live - set(EXPECTED))
    for rule, (key, _) in EXPECTED.items():
        assert key.split("|")[0] == rule


@pytest.mark.parametrize("rule", sorted(EXPECTED))
def test_v170_classifies_every_rule(rule):
    key, kind = EXPECTED[rule]
    assert _kind(key) == kind, (rule, key)


def test_v170_class_changes_vs_v072():
    """The classification deltas are exactly the R2-093 list, nothing else moved."""
    old_case = _between(_VIEW72, "           CASE\n", "           END AS ENTITY_KIND")
    assert old_case.count("THEN") == _CASE.count("THEN")                     # no arm added or removed
    moved = {}
    for (rules, kind), (old_rules, old_kind) in zip(_arms(), [
            (set(re.findall(r"'(\w+)'", m.group(1))) if m.group(1) else {m.group(2)}, m.group(3))
            for m in re.finditer(r"WHEN r\.RULE_ID (?:IN \(([^)]*)\)|= '(\w+)') THEN '(\w+)'", old_case)],
            strict=True):
        assert kind == old_kind
        if rules - old_rules:
            moved[kind] = rules - old_rules
        assert old_rules <= rules
    assert moved == {"WAREHOUSE": {"COST_IDLE_OPPORTUNITY", "COST_SLEEP_POLLING"},
                     "OBJECT": {"PIPE_DT_FAILURES", "PIPE_VOLUME_DROP", "DQ_BREACH", "DQ_SCHEMA_DRIFT"},
                     "USER": {"SEC_FAILED_LOGINS", "SEC_LOGIN_TAKEOVER", "SEC_ADMIN_GRANT", "COST_AI_USER_RUNAWAY"}}
    assert _bands() == {"DAILY", "WARN", "CRIT", "MED", "HIGH", "EXH", "ALL"}
    # owner question (R2-093): OPS_PIPELINE_DEGRADED's STALE / ERR sub-kinds stay separate SCOPE proposals
    assert not {"STALE", "ERR"} & _bands()


def test_v170_bands_cover_every_supersede_token():
    """Every band token the hourly supersede sweep escalates between is an ACCOUNT token here, so an escalated
    band never reads as an entity (V072 drifted: EXH was added to the sweep, never to the view)."""
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    scan = _latest_proc_bodies()["SP_ALERT_SCAN"]
    pairs = re.findall(r"REPLACE\(lo\.DEDUPE_KEY, '\|([A-Z]+)\|', '\|([A-Z]+)\|'\)", scan)
    tokens = {t for pair in pairs for t in pair}
    assert {"WARN", "CRIT", "MED", "HIGH", "EXH"} <= tokens
    assert tokens <= _bands()


def _out_cols(view: str) -> list[str]:
    sel = view[view.rindex("\nSELECT FAMILY || '|'"):view.rindex("\nFROM evidence;")]
    cols = []
    for ln in sel.splitlines():
        m = re.search(r"\bAS (\w+),?$", ln) or re.fullmatch(r"       (\w+),", ln)
        if m:
            cols.append(m.group(1))
    return cols


def test_v170_task_failures_no_longer_self_corroborate():
    conf = _between(_VIEW, "       CASE\n         WHEN MATCHED_WH_CHANGES", "       END AS CONFIDENCE")
    assert conf.count("IFF(FAMILY = 'PIPE_TASK_FAILURES', 0, MATCHED_TASK_FAILURES)") == 1
    assert _VIEW.count("IFF(FAMILY = 'PIPE_TASK_FAILURES', 0, MATCHED_TASK_FAILURES)") == 1
    assert "+ MATCHED_TASK_FAILURES > 0" not in _VIEW
    assert _VIEW.count("'; task failures (alert source, not corroboration)='") == 1
    # the column set and order are unchanged (the app reads SELECT *)
    assert _out_cols(_VIEW) == _out_cols(_VIEW72) and len(_out_cols(_VIEW)) == 15
    assert "COPY GRANTS" not in _VIEW and "COPY GRANTS" not in _VIEW72


def test_v170_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    stmts = list(_plain_statements(_MIG))
    assert len(stmts) == 2
    for statement in stmts:
        assert sqlglot.parse(statement, dialect="snowflake")


# -- teardown (test_teardown_coverage is name-only) ------------------------------------------------------------

def test_v170_teardown_drops_both_overloads_by_signature():
    teardown = read("snowflake/teardown.sql")
    live = [ln for ln in teardown.splitlines() if not ln.lstrip().startswith("--")]
    four = "DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE(VARCHAR, VARCHAR, VARCHAR, VARCHAR);"
    five = ("DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE(VARCHAR, VARCHAR, VARCHAR, VARCHAR, "
            "VARCHAR);")
    assert [ln for ln in live if ln.startswith(four)] and [ln for ln in live if ln.startswith(five)]
    assert "DROP VIEW IF EXISTS DBA_MAINT_DB.OVERWATCH.INCIDENT_PROPOSALS;" in live


# -- the app half: control_room ------------------------------------------------------------------------------

@pytest.mark.parametrize(("msg", "want"), [
    ("OK: declared 3f2a-11 with 3 member(s) linked", ("declared", 3)),
    ("OK: declared 0b9c-0042 with 12 member(s) linked", ("declared", 12)),
    ("DECLARED: 2 member(s) linked", ("declared", 2)),                 # the V131 proc, before V170 is applied
    ("DECLARED: 0 member(s) linked", ("empty", 0)),                    # V131 committed an empty incident
    ("NOOP: this family already has an open incident", ("family_open", None)),
    ("NOOP: no open alerts left to link - nothing declared", ("no_alerts", None)),
    ("", ("failed", None)),
    (None, ("failed", None)),
    ("(pre-V051 legacy path) ", ("failed", None)),                     # execute_action, proc missing, fallback []
    ("FAILED: x", ("failed", None)),
    ("INVALID: proposal key is required", ("failed", None)),
    ("SQL compilation error: Unknown user-defined function", ("failed", None)),
    ("Procedure returned no verdict.", ("failed", None)),
])
def test_declare_verdict_classifies_from_the_message_text_only(msg, want):
    from app.ui.pages.control_room import _declare_verdict
    assert _declare_verdict(msg) == want


def test_declare_call_builder_selects_the_overload_by_actor():
    from app.ui.pages.control_room import _incident_declare_call_sql
    four = _incident_declare_call_sql("t", "high", "ALFA", "F|x|ACCOUNT|")
    assert four.startswith("CALL DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_DECLARE(")
    assert four.endswith("'t', 'HIGH', 'ALFA', 'F|x|ACCOUNT|')")
    five = _incident_declare_call_sql("t", "HIGH", "ALFA", "F|x|ACCOUNT|", actor_sql="'JDOE'")
    assert five == four[:-1] + ", 'JDOE')"
    sqlglot = pytest.importorskip("sqlglot")
    for sql in (four, five):
        sqlglot.parse_one(sql, read="snowflake")


def _declare_block() -> str:
    i = _CR.index("write_gate_open(_exec_key)")
    return _CR[i:_CR.index("stamp_write(_exec_key, _ok_all)", i)]


def test_declare_call_site_gates_the_actor_on_v170_and_reads_the_verdict():
    body = _CR.split('elif section == "Incidents & triage":', 1)[1]
    assert "actor_sql=identity_sql() if has_migration(170, _PAGE) else None" in body
    blk = _declare_block()
    # the CALL goes through execute_action with NO fallback: the legacy two-INSERT path V131 removed never returns
    assert 'execute_action(_call + ";", [], page=_PAGE)' in blk
    assert "execute_statement(_call" not in blk and "_incident_declare_sql(" not in blk
    # classified from the message text only -- execute_action's ok flag is never read
    assert "_verdict, _n = _declare_verdict(_m)" in blk
    assert re.search(r"(\w+), _m = execute_action\(_call", blk).group(1) == "_"
    assert "_ok_all = _verdict != \"failed\"" in blk


def test_declare_logs_and_toasts_only_when_an_incident_was_declared():
    blk = _declare_block()
    branches = re.split(r"\n\s*(?=if _verdict ==|elif _verdict ==|else:)", blk[blk.index("if _verdict =="):])
    declared = [b for b in branches if b.startswith('if _verdict == "declared":')]
    assert len(declared) == 1
    assert blk.count('log_ui_event("incident_declare"') == 1 and 'log_ui_event("incident_declare"' in declared[0]
    assert blk.count("notify(True,") == 1 and "notify(True," in declared[0]
    for kind in ("family_open", "no_alerts", "empty"):
        (b,) = [b for b in branches if b.startswith(f'elif _verdict == "{kind}":')]
        assert "notify(False," in b and "log_ui_event" not in b, kind
    assert "_family_open_message(None, _prow.get(\"ALERTS\"))" in blk
    # the family-open PRE-check stays and still names the blocking incident
    assert '_incident_family_open_check_sql(str(_prow["COMPANY"]), _pick)' in _CR


def test_legacy_declare_reference_writes_the_viewer_too():
    from app.ui.pages.control_room import _incident_declare_sql
    inc, mem = _incident_declare_sql("t", "HIGH", "ALFA", "FAM|x|ACCOUNT|")
    assert "ROOT_CAUSE_KIND, DECLARED_BY)" in inc and "'UNKNOWN', CURRENT_USER() " in inc
    assert "AUTO_LINKED, LINKED_BY)" in mem and "e.RAISED_AT, FALSE, CURRENT_USER() " in mem
    body = _CR.split("def _incident_declare_sql", 1)[1].split("\ndef ", 1)[0]
    assert body.count("SELECT {inc_id},") == 2 and "identity_sql()" in body
    sqlglot = pytest.importorskip("sqlglot")
    for sql in (inc, mem):
        sqlglot.parse_one(sql, read="snowflake")


def test_declare_captions_are_schema_gated():
    body = _CR.split('elif section == "Incidents & triage":', 1)[1]
    # pre-V170 honesty: manual declares credit the app owner until the 5-arg overload exists
    assert "if not has_migration(170, _PAGE):" in body
    assert "Declared by and Linked by on a manual declare show the app owner until" in body
    # the V170 confidence rule is claimed only once applied
    assert "reach HIGH only with a matching task change" in body
    gate = body.index("reach HIGH only with a matching task change")
    assert "has_migration(170, _PAGE)" in body[gate - 400:gate]
    assert '"account-wide (the whole family)" if _entity_kind.upper() == "ACCOUNT"' in body
    assert 'f"Scope: {_scope} | confidence {_confidence}. "' in body


# -- integration lockstep (docs / Admin), landed by the V166-V172 integrator: DEPLOYMENT.md, README.md and
#    admin._EXPECTED_MIGRATIONS are shared files a single cluster does not edit; the validate tip is derived in
#    tests/test_release_lockstep.py.

def test_validate_and_docs_track_v170():
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert f"snowflake/migrations/{_NAME}" in read(rel), rel


def test_v170_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 170 in _EXPECTED_MIGRATIONS
    text = str(_EXPECTED_MIGRATIONS[170])
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in text and "$" not in text and not text.endswith(".")
