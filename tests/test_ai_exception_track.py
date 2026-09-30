"""v4.605: Cost > Chargeback & AI exceptions track through the ONE shared Track write.

Before v4.605 the page built up to 10 INSERTs of its own: de-duplicated on COMPANY + TITLE + open status +
CREATED_AT in this month, with no SOURCE_ENTITY_TYPE/KEY, OWNER 'DBA / AI Governance' and COMPANY_FOR_USER's raw
'UNKNOWN'. Now fix_queue.ai_exception_track_items turns the Exceptions table into one USER item per user (every
signal in its detail) plus one AI_BUDGET item for the all-users breach, keyed on the Company scope, and
fix_queue.track_entities_sql writes them -- at most two statements, entity-keyed, scoped to the page's SOURCE (a
Security work item on the same user never blocks), with a legacy-title arm so a still-open pre-v4.605 row is
never duplicated (no backfill, no migration). The builder's defaults stay byte-identical to v4.604 (goldens
captured on 4769af4b). The SQL runs for real in sqlite (the tests/test_track_cooldown_harness.py harness).
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager

import pandas as pd
import pytest
from test_track_cooldown_harness import _db, _to_sqlite

from app.core.query import _statement_allowed
from app.logic import fix_queue
from app.logic.fix_queue import (
    AI_SCOPE_ENTITY_TYPE,
    AI_TRACK_CAP,
    AI_TRACK_SEVERITIES,
    AI_TRACK_SOURCE,
    AI_USER_ENTITY_TYPE,
    ai_exception_track_items,
    track_entities_sql,
)
from app.logic.workbench import ENTITY_TYPES
from tests._source import read

sqlglot = pytest.importorskip("sqlglot")

_CB = "app/ui/pages/cost_parts/ai_chargeback.py"
_SCOPE_TITLE = "Cortex AI budget breach (all users): (all users) ((all sources))"


def _row(user: str, usd: float, *, source: str = "(all sources)", signal: str = "Budget breach",
         sev: str = "Critical", requests: int = 100, cpr: float = 0.05) -> dict:
    return {"SEVERITY": sev, "SIGNAL": signal, "USER_NAME": user, "SOURCE": source, "TOTAL_REQUESTS": requests,
            "CREDITS_PER_REQUEST": cpr, "PROJECTED_30D_USD": usd}


def _scope(usd: float) -> dict:
    return _row("(all users)", usd, signal="AI budget breach (all users)", requests=1000, cpr=0.01)


def _cpr(user: str, usd: float, source: str = "CLI") -> dict:
    return _row(user, usd, source=source, signal="Cost per request spike", sev="High")


def _user_sql(items: list[dict], actor: str = "CURRENT_USER()") -> str:
    return track_entities_sql(items, entity_type=AI_USER_ENTITY_TYPE, source=AI_TRACK_SOURCE,
                              actor_sql=actor, bulk=False, severities=AI_TRACK_SEVERITIES,
                              company_from_user=True, source_scoped=True)


def _scope_sql(items: list[dict], actor: str = "CURRENT_USER()") -> str:
    return track_entities_sql(items, entity_type=AI_SCOPE_ENTITY_TYPE, source=AI_TRACK_SOURCE,
                              actor_sql=actor, bulk=False, severities=AI_TRACK_SEVERITIES,
                              source_scoped=True)


# --------------------------------------------------------------------------------------- the items ----

def test_one_item_per_user_strongest_signal_first():
    ex = pd.DataFrame([_row("A", 300.0), _cpr("A", 120.0), _cpr("B", 50.0, "SNOWSIGHT")])
    groups = ai_exception_track_items(ex, "ALFA")
    assert set(groups) == {AI_USER_ENTITY_TYPE, AI_SCOPE_ENTITY_TYPE} and groups[AI_SCOPE_ENTITY_TYPE] == []
    a, b = groups[AI_USER_ENTITY_TYPE]
    assert a["ENTITY_KEY"] == "A" and a["SEVERITY"] == "CRITICAL"
    assert a["TITLE"] == "Cortex Budget breach: A ((all sources)) + 1 more signal"
    assert a["ESTIMATED_USD"] == 300.0 and a["PERIOD"] == "MONTHLY" and a["CONFIDENCE"] is None
    assert "Budget breach ((all sources)): 100 requests, projected 30d $300.00, cr/request 0.0500" in a["DETAIL"]
    assert "Cost per request spike (CLI): 100 requests, projected 30d $120.00" in a["DETAIL"]
    assert a["DETAIL"].endswith(". Projected at the time of tracking.")
    assert b["ENTITY_KEY"] == "B" and b["SEVERITY"] == "HIGH" and b["ESTIMATED_USD"] == 50.0
    assert b["TITLE"] == "Cortex Cost per request spike: B (SNOWSIGHT)"
    # a user with two spiking sources and no budget row is priced at the sum of its distinct sources
    (c,) = ai_exception_track_items(pd.DataFrame([_cpr("C", 40.0), _cpr("c", 25.0, "SNOWSIGHT"),
                                                  _cpr("C", 99.0)]), "ALL")[AI_USER_ENTITY_TYPE]
    assert c["ESTIMATED_USD"] == 65.0 and c["TITLE"].endswith(" + 2 more signals")
    # an unpriced user writes no estimate and no period
    (z,) = ai_exception_track_items(pd.DataFrame([_cpr("Z", 0.0)]), "ALL")[AI_USER_ENTITY_TYPE]
    assert z["ESTIMATED_USD"] is None and z["PERIOD"] == ""
    long = ai_exception_track_items(pd.DataFrame([_cpr("L" * 600, 1.0, "S" * 900)]), "ALL")[AI_USER_ENTITY_TYPE][0]
    assert len(long["DETAIL"]) <= 1000 and long["DETAIL"].endswith("Projected at the time of tracking.")
    assert len(long["TITLE"]) <= 300 and len(long["ENTITY_KEY"]) <= 500


def test_scope_item_is_scope_keyed_and_incremental():
    groups = ai_exception_track_items(pd.DataFrame([_scope(440.0), _row("A", 220.0), _cpr("B", 110.0)]), "ALFA")
    (agg,) = groups[AI_SCOPE_ENTITY_TYPE]
    assert agg["ENTITY_KEY"] == "ALFA" and agg["COMPANY"] == "ALFA" and agg["SEVERITY"] == "CRITICAL"
    assert agg["TITLE"] == _SCOPE_TITLE
    assert agg["DETAIL"].startswith("1,000 requests, projected 30d $440.00, cr/request 0.0100. Its estimate")
    assert agg["ESTIMATED_USD"] == 110.0 and agg["PERIOD"] == "MONTHLY"
    users = [i["ESTIMATED_USD"] for i in groups[AI_USER_ENTITY_TYPE]]
    assert agg["ESTIMATED_USD"] + sum(users) == 440.0                  # the scope total, counted once
    # the user items count all of it: the scope item is written unpriced (NULL / NULL)
    (full,) = ai_exception_track_items(pd.DataFrame([_scope(440.0), _row("A", 300.0), _row("B", 200.0)]),
                                       "ALL")[AI_SCOPE_ENTITY_TYPE]
    assert full["ESTIMATED_USD"] is None and full["PERIOD"] == "" and full["ENTITY_KEY"] == "ALL"
    # a user holding BOTH a budget row and a CPR row is subtracted once (its budget row), not twice
    (once,) = ai_exception_track_items(pd.DataFrame([_scope(440.0), _row("A", 300.0), _cpr("A", 120.0)]),
                                       "")[AI_SCOPE_ENTITY_TYPE]
    assert once["ESTIMATED_USD"] == 140.0 and once["ENTITY_KEY"] == "ALL"


def test_cap_counts_exception_rows():
    rows = [_scope(5000.0)] + [_cpr(f"U{i:02d}", 10.0) for i in range(11)]       # 12 rows
    groups = ai_exception_track_items(pd.DataFrame(rows), "ALL")
    assert AI_TRACK_CAP == 10
    assert [i["ENTITY_KEY"] for i in groups[AI_USER_ENTITY_TYPE]] == [f"U{i:02d}" for i in range(9)]
    assert len(groups[AI_SCOPE_ENTITY_TYPE]) == 1
    assert len(ai_exception_track_items(pd.DataFrame(rows), "ALL", cap=3)[AI_USER_ENTITY_TYPE]) == 2


def test_empty_and_none_give_no_items():
    for ex in (None, pd.DataFrame()):
        assert ai_exception_track_items(ex, "ALL") == {AI_USER_ENTITY_TYPE: [], AI_SCOPE_ENTITY_TYPE: []}
    blank = ai_exception_track_items(pd.DataFrame([_cpr("", 5.0), _cpr(None, 5.0)]), "ALL")
    assert blank == {AI_USER_ENTITY_TYPE: [], AI_SCOPE_ENTITY_TYPE: []}
    assert _user_sql([]) == "" and _scope_sql([]) == ""


# ------------------------------------------------------------------------------------- the SQL ----

def test_ai_user_statement_shape():
    groups = ai_exception_track_items(pd.DataFrame([_scope(500.0), _row("JDOE", 300.0), _cpr("XJDOE", 50.0)]),
                                      "ALFA")
    sql = _user_sql(groups[AI_USER_ENTITY_TYPE])
    assert ("SELECT COALESCE(NULLIF(DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(v.ENTITY_KEY), 'UNKNOWN'), 'ALL'), "
            "v.SEVERITY") in sql
    assert "'CRITICAL'" in sql and "'HIGH'" in sql and "'MONTHLY'" in sql
    assert "'UNASSIGNED', 'OPEN', 'Cost Intelligence > Chargeback & AI > AI users',\n       'USER'" in sql
    assert ("    WHERE q.SOURCE = 'Cost Intelligence > Chargeback & AI > AI users'\n"
            "      AND ((UPPER(q.SOURCE_ENTITY_TYPE) = 'USER'\n"
            "            AND UPPER(q.SOURCE_ENTITY_KEY) = UPPER(v.ENTITY_KEY))\n"
            "           OR (q.SOURCE_ENTITY_TYPE IS NULL\n"
            "               AND CONTAINS(UPPER(q.TITLE), ': ' || UPPER(v.ENTITY_KEY) || ' (')))\n"
            "      AND (UPPER(q.STATUS) IN ('OPEN', 'IN_PROGRESS')))") in sql
    scope = _scope_sql(groups[AI_SCOPE_ENTITY_TYPE])
    assert "SELECT v.COMPANY, v.SEVERITY" in scope and "COMPANY_FOR_USER" not in scope
    assert ("OR (q.SOURCE_ENTITY_TYPE IS NULL\n               AND CONTAINS(UPPER(q.TITLE), ': (ALL USERS) (') "
            "AND UPPER(COALESCE(q.COMPANY, '')) = UPPER(v.ENTITY_KEY)))") in scope
    for stmt in (sql, scope):
        assert _statement_allowed(stmt) == (True, "")
        parsed = sqlglot.parse(stmt, read="snowflake")
        assert len(parsed) == 1 and parsed[0].key == "insert"
        assert "DATE_TRUNC('month'" not in stmt and "q.TITLE =" not in stmt
        assert "DBA / AI Governance" not in stmt and "q.CREATED_AT" not in stmt
        assert read("app/logic/fix_queue.py").count("INSERT INTO") == 1
    # source-scoped without a legacy arm (a type with no pre-v4.605 rows): entity match only, still scoped
    task = track_entities_sql([{"ENTITY_KEY": "DB.S.T"}], entity_type="TASK", source="X", actor_sql="'J'",
                              bulk=False, source_scoped=True)
    assert "WHERE q.SOURCE = 'X'\n      AND UPPER(q.SOURCE_ENTITY_TYPE) = 'TASK'" in task and "CONTAINS" not in task


# Captured on main 4769af4b (v4.604.0) BEFORE the v4.605 options existed: with the defaults, the shared Track
# write Operations > Optimize and Control Room triage use is byte-identical.
_GOLDEN_ITEMS = [
    {"COMPANY": "ALFA", "SEVERITY": "medium", "TITLE": "Fix: FP1 (DB)", "DETAIL": "It's d", "ENTITY_KEY": "FP1",
     "CONFIDENCE": 0.75, "ESTIMATED_USD": 12.345, "PERIOD": "MONTHLY"},
    {"COMPANY": "", "SEVERITY": "HIGH", "TITLE": "T2", "DETAIL": "d2", "ENTITY_KEY": "fp2",
     "CONFIDENCE": None, "ESTIMATED_USD": None, "PERIOD": ""},
    {"COMPANY": "Trexis", "SEVERITY": "LOW", "TITLE": "T3", "DETAIL": "d3", "ENTITY_KEY": ""},
]
_GOLDEN_FAMILY_BULK_REBROKE = (
    'INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE\n'
    '    (COMPANY, SEVERITY, TITLE, DETAIL, OWNER, STATUS, SOURCE, SOURCE_ENTITY_TYPE,\n'
    '     SOURCE_ENTITY_KEY, CONFIDENCE, ESTIMATED_USD, PERIOD, UPDATED_BY)\n'
    "SELECT v.COMPANY, v.SEVERITY, v.TITLE, v.DETAIL, 'UNASSIGNED', 'OPEN', 'Operations > Optimize',\n"
    "       'QUERY_FINGERPRINT', v.ENTITY_KEY, v.CONF::FLOAT, v.USD::NUMBER(18,2),\n"
    "       NULLIF(v.PER, ''), CURRENT_USER()\n"
    'FROM (VALUES\n'
    "    ('ALFA', 'MEDIUM', 'Fix: FP1 (DB)', 'It''s d', 'FP1', 0.75, 12.35, 'MONTHLY'),\n"
    "    ('ALL', 'LOW', 'T2', 'd2', 'fp2', NULL, NULL, '')\n"
    ') AS v (COMPANY, SEVERITY, TITLE, DETAIL, ENTITY_KEY, CONF, USD, PER)\n'
    'WHERE NOT EXISTS (\n'
    '    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE q\n'
    "    WHERE UPPER(q.SOURCE_ENTITY_TYPE) = 'QUERY_FINGERPRINT'\n"
    '      AND UPPER(q.SOURCE_ENTITY_KEY) = UPPER(v.ENTITY_KEY)\n'
    "      AND (UPPER(q.STATUS) IN ('OPEN', 'IN_PROGRESS')\n"
    "           OR (UPPER(q.STATUS) IN ('DROPPED', 'DONE')\n"
    "               AND COALESCE(q.COMPLETED_AT, q.UPDATED_AT) >= DATEADD('day', -90, CURRENT_TIMESTAMP())\n"
    "               AND NOT (UPPER(q.STATUS) = 'DONE' AND UPPER(v.ENTITY_KEY) IN ('FP1', 'FP9')))))"
)
_GOLDEN_FAMILY_SINGLE = (
    'INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE\n'
    '    (COMPANY, SEVERITY, TITLE, DETAIL, OWNER, STATUS, SOURCE, SOURCE_ENTITY_TYPE,\n'
    '     SOURCE_ENTITY_KEY, CONFIDENCE, ESTIMATED_USD, PERIOD, UPDATED_BY)\n'
    "SELECT v.COMPANY, v.SEVERITY, v.TITLE, v.DETAIL, 'UNASSIGNED', 'OPEN', 'Operations > Optimize',\n"
    "       'QUERY_FINGERPRINT', v.ENTITY_KEY, v.CONF::FLOAT, v.USD::NUMBER(18,2),\n"
    "       NULLIF(v.PER, ''), CURRENT_USER()\n"
    'FROM (VALUES\n'
    "    ('ALFA', 'MEDIUM', 'Fix: FP1 (DB)', 'It''s d', 'FP1', 0.75, 12.35, 'MONTHLY'),\n"
    "    ('ALL', 'LOW', 'T2', 'd2', 'fp2', NULL, NULL, '')\n"
    ') AS v (COMPANY, SEVERITY, TITLE, DETAIL, ENTITY_KEY, CONF, USD, PER)\n'
    'WHERE NOT EXISTS (\n'
    '    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE q\n'
    "    WHERE UPPER(q.SOURCE_ENTITY_TYPE) = 'QUERY_FINGERPRINT'\n"
    '      AND UPPER(q.SOURCE_ENTITY_KEY) = UPPER(v.ENTITY_KEY)\n'
    "      AND (UPPER(q.STATUS) IN ('OPEN', 'IN_PROGRESS')))"
)
_GOLDEN_TASK_SINGLE = (
    'INSERT INTO DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE\n'
    '    (COMPANY, SEVERITY, TITLE, DETAIL, OWNER, STATUS, SOURCE, SOURCE_ENTITY_TYPE,\n'
    '     SOURCE_ENTITY_KEY, CONFIDENCE, ESTIMATED_USD, PERIOD, UPDATED_BY)\n'
    "SELECT v.COMPANY, v.SEVERITY, v.TITLE, v.DETAIL, 'UNASSIGNED', 'OPEN', 'Control Room > Triage',\n"
    "       'TASK', v.ENTITY_KEY, v.CONF::FLOAT, v.USD::NUMBER(18,2),\n"
    "       NULLIF(v.PER, ''), CURRENT_USER()\n"
    'FROM (VALUES\n'
    "    ('ALFA', 'MEDIUM', 'Fix: FP1 (DB)', 'It''s d', 'FP1', 0.75, 12.35, 'MONTHLY'),\n"
    "    ('ALL', 'LOW', 'T2', 'd2', 'fp2', NULL, NULL, '')\n"
    ') AS v (COMPANY, SEVERITY, TITLE, DETAIL, ENTITY_KEY, CONF, USD, PER)\n'
    'WHERE NOT EXISTS (\n'
    '    SELECT 1 FROM DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE q\n'
    "    WHERE UPPER(q.SOURCE_ENTITY_TYPE) = 'TASK'\n"
    '      AND UPPER(q.SOURCE_ENTITY_KEY) = UPPER(v.ENTITY_KEY)\n'
    "      AND (UPPER(q.STATUS) IN ('OPEN', 'IN_PROGRESS')))"
)


def test_defaults_are_byte_identical_to_v4604():
    assert track_entities_sql(_GOLDEN_ITEMS, entity_type="QUERY_FINGERPRINT", source="Operations > Optimize",
                              actor_sql="CURRENT_USER()", bulk=True,
                              rebroke_keys={"fp1", "FP9"}) == _GOLDEN_FAMILY_BULK_REBROKE
    assert track_entities_sql(_GOLDEN_ITEMS, entity_type="QUERY_FINGERPRINT", source="Operations > Optimize",
                              actor_sql="CURRENT_USER()", bulk=False) == _GOLDEN_FAMILY_SINGLE
    assert track_entities_sql(_GOLDEN_ITEMS, entity_type="TASK", source="Control Room > Triage",
                              actor_sql="CURRENT_USER()", bulk=False) == _GOLDEN_TASK_SINGLE
    # the explicit defaults are the same statement
    assert track_entities_sql(_GOLDEN_ITEMS, entity_type="TASK", source="Control Room > Triage",
                              actor_sql="CURRENT_USER()", bulk=False, severities=fix_queue.TRACK_SEVERITIES,
                              company_from_user=False, source_scoped=False) == _GOLDEN_TASK_SINGLE


def test_new_options_are_type_guarded():
    for kind in ("TASK", "QUERY_FINGERPRINT", "WAREHOUSE", AI_SCOPE_ENTITY_TYPE):
        with pytest.raises(ValueError):
            track_entities_sql([{"ENTITY_KEY": "K"}], entity_type=kind, source="X", actor_sql="'J'", bulk=False,
                               company_from_user=True)
    item = {"ENTITY_KEY": "K", "SEVERITY": "URGENT"}
    ai = track_entities_sql([item], entity_type=AI_USER_ENTITY_TYPE, source="X", actor_sql="'J'", bulk=False,
                            severities=AI_TRACK_SEVERITIES)
    assert "'K'" in ai and "'LOW'" in ai and "'URGENT'" not in ai               # outside the set -> LOW
    crit = {"ENTITY_KEY": "K", "SEVERITY": "critical"}
    assert "'CRITICAL'" in track_entities_sql([crit], entity_type="USER", source="X", actor_sql="'J'",
                                              bulk=False, severities=AI_TRACK_SEVERITIES)
    # the default set still clamps: a CRITICAL item on the Optimize / triage path writes LOW
    assert "'CRITICAL'" not in track_entities_sql([crit], entity_type="TASK", source="X", actor_sql="'J'",
                                                  bulk=False)


def test_ai_values_stay_literals():
    evil = "x'); DROP TABLE ACTION_QUEUE; --\\"
    ex = pd.DataFrame([_scope(900.0), _row(evil, 300.0, source=evil, signal=evil), _cpr(evil.lower(), 5.0)])
    groups = ai_exception_track_items(ex, evil)
    for sql in (_user_sql(groups[AI_USER_ENTITY_TYPE]), _scope_sql(groups[AI_SCOPE_ENTITY_TYPE])):
        assert _statement_allowed(sql) == (True, "")
        parsed = sqlglot.parse(sql, read="snowflake")
        assert len(parsed) == 1 and parsed[0].key == "insert"
        residue = re.sub(r"'(?:[^'\\]|\\.|'')*'", "''", sql)
        assert "DROP" not in residue.upper()


# ------------------------------------------------------------------------------ executed in sqlite ----

_AQ_COLS = ("COMPANY", "SEVERITY", "TITLE", "DETAIL", "OWNER", "STATUS", "SOURCE", "SOURCE_ENTITY_TYPE",
            "SOURCE_ENTITY_KEY", "UPDATED_BY", "CREATED_AT", "UPDATED_AT")


def _ai_db() -> sqlite3.Connection:
    con = _db([])
    con.create_function("CONTAINS", 2, lambda s, sub: None if s is None or sub is None else int(sub in s))
    con.create_function("COMPANY_FOR_USER", 1, lambda u: {"NEWUSER": "Trexis", "XJDOE": "ALFA"}.get(u, "UNKNOWN"))
    seed = [
        # a pre-v4.605 row (no entity key) still open: blocks JDOE, never XJDOE / DOE
        ("UNKNOWN", "CRITICAL", "Cortex Budget breach: JDOE ((all sources))", "OPEN", AI_TRACK_SOURCE, None, None),
        # a pre-v4.605 row already DONE: never blocks (bulk=False, as before)
        ("UNKNOWN", "HIGH", "Cortex Cost per request spike: DONEUSER (CLI)", "DONE", AI_TRACK_SOURCE, None, None),
        # an entity-keyed open AI item: blocks ENTUSER (the key match is case-insensitive)
        ("ALFA", "HIGH", "Cortex High usage: ENTUSER ((all sources))", "OPEN", AI_TRACK_SOURCE, "USER", "entuser"),
        # an open SECURITY work item on SECUSER: another source, never blocks the AI item
        ("ALFA", "HIGH", "Security exception", "OPEN", "Security decision queue", "USER", "SECUSER"),
        # the pre-v4.605 all-users row for the ALFA scope: blocks the ALFA scope item, not the ALL one
        ("ALFA", "CRITICAL", _SCOPE_TITLE, "IN_PROGRESS", AI_TRACK_SOURCE, None, None),
    ]
    for company, sev, title, status, source, etype, ekey in seed:
        con.execute(f"INSERT INTO ACTION_QUEUE ({', '.join(_AQ_COLS)}) VALUES (?, ?, ?, 'd', 'DBA / AI Governance', "
                    "?, ?, ?, ?, 'SEED', '2026-08-01 00:00:00', '2026-08-01 00:00:00')",
                    (company, sev, title, status, source, etype, ekey))
    return con


def _new_rows(con: sqlite3.Connection) -> list[tuple]:
    return con.execute("SELECT SOURCE_ENTITY_TYPE, SOURCE_ENTITY_KEY, COMPANY, SEVERITY, OWNER, STATUS, SOURCE, "
                       "PERIOD, UPDATED_BY FROM ACTION_QUEUE WHERE UPDATED_BY <> 'SEED' "
                       "ORDER BY SOURCE_ENTITY_TYPE, SOURCE_ENTITY_KEY").fetchall()


def test_ai_track_executes_in_sqlite():
    users = pd.DataFrame([_scope(2000.0), _row("JDOE", 300.0), _row("XJDOE", 250.0), _cpr("DOE", 90.0),
                          _cpr("DONEUSER", 80.0), _row("ENTUSER", 70.0, sev="High", signal="Budget concentration"),
                          _cpr("SECUSER", 60.0), _row("NEWUSER", 50.0, sev="Medium", signal="High usage")])
    alfa = ai_exception_track_items(users, "ALFA")
    everyone = ai_exception_track_items(users, "ALL")
    con = _ai_db()
    stmts = [_user_sql(alfa[AI_USER_ENTITY_TYPE], actor="'VIEWER'"),
             _scope_sql(alfa[AI_SCOPE_ENTITY_TYPE] + everyone[AI_SCOPE_ENTITY_TYPE], actor="'VIEWER'")]
    for stmt in stmts:
        con.execute(_to_sqlite(stmt))
    rows = _new_rows(con)
    assert [(t, k) for t, k, *_ in rows] == [
        ("AI_BUDGET", "ALL"),
        ("USER", "DOE"), ("USER", "DONEUSER"), ("USER", "NEWUSER"), ("USER", "SECUSER"), ("USER", "XJDOE")]
    by_key = {k: r for _t, k, *r in rows}
    assert by_key["NEWUSER"][0] == "Trexis" and by_key["XJDOE"][0] == "ALFA"   # COMPANY_FOR_USER
    assert by_key["DOE"][0] == "ALL" and by_key["SECUSER"][0] == "ALL"         # UNKNOWN reads ALL
    assert by_key["ALL"][0] == "ALL"                                           # the scope item keeps the scope
    assert {tuple(r[2:5]) for r in by_key.values()} == {("UNASSIGNED", "OPEN", AI_TRACK_SOURCE)}
    assert by_key["XJDOE"][1] == "CRITICAL" and by_key["DOE"][1] == "HIGH" and by_key["NEWUSER"][1] == "MEDIUM"
    assert by_key["ALL"][1] == "CRITICAL"
    assert {r[5] for r in by_key.values()} == {"MONTHLY"} and {r[6] for r in by_key.values()} == {"VIEWER"}
    # idempotent: the same click again inserts nothing
    for stmt in stmts:
        con.execute(_to_sqlite(stmt))
    assert len(_new_rows(con)) == len(rows)


# ---------------------------------------------------------------------------------------- the page ----

def test_chargeback_uses_the_one_track_write():
    cb = read(_CB)
    assert "INSERT INTO {core_object('ACTION_QUEUE')}" not in cb
    assert "cortex_queue_exec" not in cb and "_other_proj" not in cb and "DBA / AI Governance" not in cb
    body = cb.split("def _track_exceptions_expander", 1)[1].split("\ndef ", 1)[0]
    assert "_track_exceptions_expander(exceptions, company, is_operator)" in cb
    assert "ai_exception_track_items(exceptions, company)" in body
    assert "entity_type=AI_USER_ENTITY_TYPE" in body and "entity_type=AI_SCOPE_ENTITY_TYPE" in body
    assert body.count("source_scoped=True") == 2 and body.count("company_from_user=True") == 1
    assert body.count("severities=AI_TRACK_SEVERITIES") == 2 and body.count("bulk=False") == 2
    assert re.search(r'st\.button\("Track in Action Center", key="cortex_track_exec"\)\s*\n\s*'
                     r'and write_gate_open\("cortex_track_exec"\)\):', body)
    assert 'stamp_write("cortex_track_exec", ok_all)  # C48' in body and "st.rerun" not in body
    assert body.index("st.code(_stmt") < body.index("st.button(")
    caps = re.findall(r'st\.caption\((.*?)\)\n', body, re.S)
    assert len(caps) == 3 and not any("$" in c for c in caps)          # no dollar sign in the new captions
    assert ('st.caption("Copy and run as SNOW_ACCOUNTADMINS / SNOW_SYSADMINS - in-app execution needs an admin '
            'profile.")') in body


def test_ai_budget_never_drills_into_entity_360():
    assert AI_SCOPE_ENTITY_TYPE not in ENTITY_TYPES and AI_USER_ENTITY_TYPE in ENTITY_TYPES
    assert "if entity_type in ENTITY_TYPES and entity_key and st.button(" in read("app/ui/workbench.py")
    ds = read("app/ui/decision_studio.py")
    assert "and str(kind).strip().upper() in ENTITY_TYPES):" in ds
    assert "from app.logic.workbench import ENTITY_TYPES," in ds


class _FakeSt:
    def __init__(self, *, click: bool):
        self.calls: list[tuple[str, str]] = []
        self._click = click

    @contextmanager
    def expander(self, label, *_a, **_k):
        self.calls.append(("expander", label))
        yield self

    def caption(self, text, *_a, **_k):
        self.calls.append(("caption", str(text)))

    def code(self, text, *_a, **_k):
        self.calls.append(("code", str(text)))

    def button(self, _label, *_a, key: str = "", **_k):
        self.calls.append(("button", key))
        return self._click


def _render(monkeypatch, *, operator: bool = True, fail: str = "", click: bool = True):
    from app.ui.pages.cost_parts import ai_chargeback as cb
    fake = _FakeSt(click=click)
    seen: dict = {"ran": [], "stamps": [], "notify": []}

    def fake_statement(sql, **_k):
        kind = "AI_BUDGET" if "'AI_BUDGET'" in sql else "USER"
        seen["ran"].append(kind)
        return (False, f"boom-{kind}") if kind == fail else (True, "")

    monkeypatch.setattr(cb, "st", fake)
    monkeypatch.setattr(cb, "identity_sql", lambda: "CURRENT_USER()")
    monkeypatch.setattr(cb, "execute_statement", fake_statement)
    monkeypatch.setattr(cb, "write_gate_open", lambda key: key == "cortex_track_exec")
    monkeypatch.setattr(cb, "stamp_write", lambda key, ok: seen["stamps"].append((key, ok)))
    monkeypatch.setattr(cb, "notify", lambda ok, msg: seen["notify"].append((ok, msg)))
    ex = pd.DataFrame([_scope(440.0), _row("A", 220.0), _cpr("B", 110.0)])
    cb._track_exceptions_expander(ex, "ALFA", operator)
    return fake, seen


def test_track_click_runs_both_statements_and_reports_a_partial_write(monkeypatch):
    fake, seen = _render(monkeypatch)
    assert seen["ran"] == ["USER", "AI_BUDGET"] and seen["stamps"] == [("cortex_track_exec", True)]
    ((ok, msg),) = seen["notify"]
    assert ok and "idempotent" in msg
    kinds = [k for k, _ in fake.calls]
    assert kinds.count("code") == 2 and kinds.index("code") < kinds.index("button")
    assert fake.calls[0] == ("expander", "Track top exceptions as work items")
    caps = "\n".join(t for k, t in fake.calls if k == "caption")
    assert "Tracks the first 10 rows above into Action Center" in caps and "for the ALFA scope" in caps
    # the user statement lands, the scope one fails: the receipt says which
    _, seen = _render(monkeypatch, fail="AI_BUDGET")
    assert seen["ran"] == ["USER", "AI_BUDGET"] and seen["stamps"] == [("cortex_track_exec", False)]
    assert seen["notify"] == [(False, "The user items were tracked, but the all-users budget item was not: "
                                      "boom-AI_BUDGET")]
    # the user statement fails: stop there, the scope statement never runs
    _, seen = _render(monkeypatch, fail="USER")
    assert seen["ran"] == ["USER"] and seen["notify"] == [(False, "boom-USER")]
    # a reader sees the SQL and the copy note, never a button or a write
    fake, seen = _render(monkeypatch, operator=False)
    assert seen["ran"] == [] and not any(k == "button" for k, _ in fake.calls)
    assert "in-app execution needs an admin profile" in "\n".join(t for _k, t in fake.calls)
    # no click: nothing runs, nothing is stamped
    _, seen = _render(monkeypatch, click=False)
    assert seen["ran"] == [] and seen["stamps"] == [] and seen["notify"] == []
