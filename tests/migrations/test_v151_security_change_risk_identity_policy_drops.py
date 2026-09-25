"""V151 locks: Terraform-role DROP USER / ROLE / POLICY re-surface in the CHANGE RISK queue.

Next-Fifty rank 8 (confirmed defect). V088 excluded EVERY CHANGE_KIND='DESTRUCTIVE' row by a TF_*
role or on DBA_MAINT_DB.PUBLIC. SP_LOAD_SECURITY_FACTS (V105) tests DROP%/TRUNCATE% before
%POLICY%/%USER%, so DROP_USER / DROP_ROLE / DROP ... POLICY are DESTRUCTIVE too -- and V088 hid them
all. V151 re-derives V_SECURITY_EXCEPTION_QUEUE from V088, byte-identical except the exclusion
block, keeping a row whose QUERY_TYPE names USER/ROLE/POLICY or whose statement opens with one of
17 keyword-anchored DROP openers. Never a bare POLICY substring (insurance FACT_POLICY tables) and
never a DATABASE_NAME IS NOT NULL gate (NULL-database drops were flood). Byte-locked to
outputs/gen_v151.py; the app diagnostic's keep lists are locked to the view's literals.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from app.data import security_sql
from app.logic.security import domain_posture

_ROOT = Path(__file__).resolve().parents[2]
_MIGDIR = _ROOT / "snowflake" / "migrations"
_V151_NAME = "V151__security_change_risk_identity_policy_drops.sql"
_V151 = (_MIGDIR / _V151_NAME).read_text(encoding="utf-8")
_V088 = (_MIGDIR / "V088__security_change_risk_etl_exclusion_broadened.sql").read_text(encoding="utf-8")

_V088_BLOCK_START = "\n      -- V088: V080's fixed 18-role list"
_V151_BLOCK_START = "\n      -- V088 excluded DESTRUCTIVE (DROP/TRUNCATE) rows"
_BLOCK_END = "\n), open_actions AS ("
_VIEW_RE = re.compile(r"CREATE OR REPLACE VIEW DBA_MAINT_DB\.OVERWATCH\.V_SECURITY_EXCEPTION_QUEUE AS.*?;\n", re.S)


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _view(text: str) -> str:
    m = _VIEW_RE.search(text)
    assert m, "V_SECURITY_EXCEPTION_QUEUE not found"
    return m.group(0)


def _block(view: str, start: str) -> str:
    i = view.index(start)
    return view[i:view.index(_BLOCK_END, i)]


def _keep_lists(sql: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The two keep lists, parsed OUT of a SQL text (the V151 view or the app builder)."""
    types = re.search(r"COALESCE\(QUERY_TYPE, ''\) ILIKE ANY \(([^)]*)\)", sql)
    prev = re.search(r"'\[\[:space:\]\]\+', ' '\)\)\s+LIKE ANY \(([^)]*)\)", sql, re.S)
    assert types and prev, "keep lists not found"
    return (tuple(re.findall(r"'([^']*)'", types.group(1))),
            tuple(re.findall(r"'([^']*)'", prev.group(1))))


# ---------------------------------------------------------------------------------------------
# generation + shape
# ---------------------------------------------------------------------------------------------
def test_v151_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v151.py")],
        env={**os.environ, "V151_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _V151, (
        "V151 drifted from its forward-generation — edit outputs/gen_v151.py, not the .sql."
    )


def test_v151_is_one_guarded_view_swap():
    assert "EXCEPTION (-20151, 'V151 requires V150 first" in _V151 and "IF (v < 150) THEN" in _V151
    assert "SELECT 151 AS VERSION" in _V151 and "WHERE VERSION = 151)" in _V151
    assert _V151.count("CREATE OR REPLACE VIEW") == 1
    for forbidden in ("CREATE OR REPLACE PROCEDURE", "CREATE TABLE", "CREATE TASK", "ALTER TASK",
                      "CREATE WAREHOUSE", "RESOURCE MONITOR", "DELETE FROM", "CALL ", "ALTER TABLE"):
        assert forbidden not in _V151, forbidden
    # guard -> marker -> view -> version row, in that order
    order = [_V151.index("RAISE not_ready"),
             _V151.index("-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  (from V088;"),
             _V151.index("CREATE OR REPLACE VIEW"),
             _V151.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")]
    assert order == sorted(order)


def test_v151_marker_names_the_true_previous_definer():
    """The round-13 class: a re-derivation must start from the CURRENT definer. Lineage of the view
    is computed from the migration set itself (V075 -> V080 -> V088 -> V151)."""
    definers = sorted(
        int(p.name[1:4]) for p in _MIGDIR.glob("V*.sql")
        if re.search(r"CREATE\s+OR\s+REPLACE\s+VIEW\s+DBA_MAINT_DB\.OVERWATCH\.V_SECURITY_EXCEPTION_QUEUE\b",
                     p.read_text(encoding="utf-8"))
    )
    assert 151 in definers
    prev = max(v for v in definers if v < 151)
    assert prev == 88, f"previous definer is V{prev:03d}, not V088 -- re-derive from it"
    marker = re.search(r"^-- >>> derived:V_SECURITY_EXCEPTION_QUEUE  \(from V(\d{3});", _V151, re.M)
    assert marker and int(marker.group(1)) == prev


def test_v151_is_byte_identical_to_v088_outside_the_exclusion_block():
    base, derived = _view(_V088), _view(_V151)
    old, new = _block(base, _V088_BLOCK_START), _block(derived, _V151_BLOCK_START)
    assert base.count(old) == 1 and derived.count(new) == 1
    assert derived.replace(new, old) == base, "V151 view differs from V088 outside the exclusion block"
    # the V075/V088 arms, the 7d + RISK_SCORE>=70 WHERE, open_actions and the final join survive
    for anchor in ("'IDENTITY' AS DOMAIN", "'PRIVILEGE', 'ALL'", "'TRUST CENTER', 'ALL'",
                   "'CHANGE RISK', COMPANY",
                   "WHERE EVENT_TS >= DATEADD('day', -7, CURRENT_TIMESTAMP()) AND RISK_SCORE >= 70",
                   "FROM DBA_MAINT_DB.OVERWATCH.ACTION_QUEUE",
                   "COALESCE(a.ACTION_STATUS, 'UNTRACKED') AS STATUS"):
        assert derived.count(anchor) == 1, anchor
    assert "COALESCE(ROLE_NAME, '') IN (" not in derived   # V080's fixed role list stays superseded


def test_v151_keeps_the_v088_etl_scope_and_null_safety():
    blk = _block(_view(_V151), _V151_BLOCK_START)
    assert blk.count("CHANGE_KIND = 'DESTRUCTIVE' AND (") == 1
    assert "UPPER(COALESCE(ROLE_NAME, '')) LIKE 'TF~_%' ESCAPE '~'" in blk
    assert "UPPER(COALESCE(DATABASE_NAME, '')) = 'DBA_MAINT_DB'" in blk
    assert "AND UPPER(COALESCE(SCHEMA_NAME, '')) = 'PUBLIC'))" in blk
    assert "COALESCE(QUERY_TYPE, '')" in blk and "COALESCE(QUERY_PREVIEW, '')" in blk
    assert "AND ROLE_NAME LIKE" not in blk
    # the rec's DATABASE_NAME IS NOT NULL gate is deliberately NOT added (NULL-db TF_* drops were flood)
    assert "DATABASE_NAME IS NOT NULL" not in blk and "IS NOT NULL" not in blk
    # no bare POLICY substring on the statement text (FACT_POLICY-style ETL tables): the only
    # '%POLICY%' is the QUERY_TYPE ILIKE ANY entry; the preview list is keyword-anchored.
    assert blk.count("'%POLICY%'") == 1
    assert "COALESCE(QUERY_TYPE, '') ILIKE ANY ('%USER%', '%ROLE%', '%POLICY%')" in blk
    assert not re.search(r"QUERY_PREVIEW[^\n]*I?LIKE\s+'%", blk)
    assert "'[[:space:]]+'" in blk and "\\" not in blk          # POSIX class, no backslash in SQL
    assert ";" not in blk
    assert "'" not in "".join(ln for ln in blk.splitlines() if ln.strip().startswith("--"))


def test_v151_keep_list_is_17_anchored_openers_with_backup_policy():
    types, previews = _keep_lists(_view(_V151))
    assert types == ("%USER%", "%ROLE%", "%POLICY%")
    assert len(previews) == 17 == len(set(previews))            # 4 identity + 13 policy kinds (O-1)
    assert previews[:4] == ("DROP USER %", "DROP ROLE %", "DROP DATABASE ROLE %", "DROP APPLICATION ROLE %")
    assert all(p.startswith("DROP ") and p.endswith(" POLICY %") for p in previews[4:])
    assert previews.index("DROP BACKUP POLICY %") == previews.index("DROP STORAGE LIFECYCLE POLICY %") + 1
    assert all("_" not in p for p in types + previews)          # no ESCAPE needed


def test_v088_block_had_no_keep_test_proving_the_defect():
    blk = _block(_view(_V088), _V088_BLOCK_START)
    assert "QUERY_TYPE" not in blk and "QUERY_PREVIEW" not in blk
    # ...while its comment claimed POLICY by these roles still surfaces -- false for DROP ... POLICY
    assert "GRANT/REVOKE/POLICY by these roles" in blk


def test_v151_description_does_not_count_kinds():
    desc = re.search(r"SELECT 151 AS VERSION,\s*'((?:[^']|'')*)' AS DESCRIPTION", _V151, re.S)
    assert desc and "DROP (kind) POLICY" in desc.group(1)
    assert not re.search(r"\b1[2-7] (?:named )?(?:POLICY )?kinds\b", desc.group(1))


# ---------------------------------------------------------------------------------------------
# semantic truth table -- a Python mirror of the WHOLE pipeline into the CHANGE RISK arm:
# SP_LOAD_SECURITY_FACTS admission + classifier + score (its current definer, text-locked below),
# the view's RISK_SCORE >= 70 gate, then the V151 exclusion with keep lists parsed FROM the view.
# ---------------------------------------------------------------------------------------------
# Normalized (whitespace-collapsed) V105 text the mirror models. If a later migration re-scores or
# re-classifies, test_mirror_models_the_current_security_facts_classifier fails: update both.
_V105_ADMIT = (
    "AND (QUERY_TYPE IN ('CREATE', 'CREATE_TABLE', 'CREATE_VIEW', 'CREATE_TABLE_AS_SELECT', 'ALTER', "
    "'ALTER_TABLE_MODIFY_COLUMN', 'ALTER_SESSION', 'DROP', 'GRANT', 'REVOKE', 'RENAME', 'RENAME_TABLE', "
    "'TRUNCATE_TABLE', 'ALTER_USER', 'CREATE_USER', 'DROP_USER', 'CREATE_ROLE', 'ALTER_ROLE', 'DROP_ROLE') "
    "OR QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' OR QUERY_TYPE ILIKE '%ROLE%' "
    "OR QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%')"
)
_V105_KIND = (
    "CASE WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 'DESTRUCTIVE' "
    "WHEN QUERY_TYPE IN ('GRANT', 'REVOKE') THEN 'PRIVILEGE' "
    "WHEN QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' THEN 'SECURITY POLICY' "
    "WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 'ALTER' "
    "WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT') AND QUERY_TEXT ILIKE '%OR REPLACE%' "
    "THEN 'DESTRUCTIVE' ELSE 'CREATE' END AS CHANGE_KIND"
)
_V105_SCORE = (
    "LEAST(100, CASE WHEN QUERY_TYPE ILIKE 'DROP%' OR QUERY_TYPE ILIKE 'TRUNCATE%' THEN 90 "
    "WHEN QUERY_TYPE IN ('GRANT', 'REVOKE') THEN 80 "
    "WHEN QUERY_TYPE ILIKE '%POLICY%' OR QUERY_TYPE ILIKE '%USER%' THEN 85 "
    "WHEN QUERY_TYPE ILIKE 'ALTER%' OR QUERY_TYPE ILIKE 'RENAME%' THEN 55 "
    "WHEN QUERY_TYPE IN ('CREATE_TABLE', 'CREATE_TABLE_AS_SELECT') AND QUERY_TEXT ILIKE '%OR REPLACE%' "
    "THEN 55 ELSE 30 END + IFF(ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS'), 10, 0) "
    "+ IFF(COALESCE(DATABASE_NAME, '') ILIKE '%PROD%', 10, 0) ) AS RISK_SCORE"
)
_ADMIT_TYPES = frozenset(re.findall(r"'([A-Z_]+)'", _V105_ADMIT.split(") OR", 1)[0]))
_ADMINS = ("ACCOUNTADMIN", "SNOW_ACCOUNTADMINS")


def _norm(s: str) -> str:
    return " ".join(s.split())


def _latest_security_facts_body() -> tuple[int, str]:
    pat = re.compile(r"CREATE\s+OR\s+REPLACE\s+PROCEDURE\s+DBA_MAINT_DB\.OVERWATCH\.SP_LOAD_SECURITY_FACTS\s*\(")
    best: tuple[int, str] | None = None
    for p in _MIGDIR.glob("V*.sql"):
        text = p.read_text(encoding="utf-8")
        for m in pat.finditer(text):
            rest = text[m.end():]
            a = rest.index("$$")
            body = rest[a + 2: rest.index("$$", a + 2)]
            v = int(p.name[1:4])
            if best is None or v > best[0]:
                best = (v, body)
    assert best is not None
    return best


def test_mirror_models_the_current_security_facts_classifier():
    v, body = _latest_security_facts_body()
    nb = _norm(body)
    # both reload arms (d<=3 from OW_QH_EXTRACT, d>3 from ACCOUNT_USAGE) carry the identical text
    for label, text in (("admission", _V105_ADMIT), ("CHANGE_KIND", _V105_KIND), ("RISK_SCORE", _V105_SCORE)):
        assert nb.count(text) == 2, (
            f"SP_LOAD_SECURITY_FACTS (V{v:03d}) {label} no longer matches the truth-table mirror -- "
            "update _surfaces() and the _V105_* texts together")
    assert "LEFT(QUERY_TEXT, 200) AS QUERY_PREVIEW" in body


def _like(value: str, pattern: str, *, ci: bool) -> bool:
    rx = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, value, re.S | (re.I if ci else 0)) is not None


def _surfaces(role: str | None, qtype: str, db: str | None, schema: str | None, text: str) -> bool:
    """True when the statement lands in the V151 CHANGE RISK arm (and so in the queue)."""
    # SP_LOAD_SECURITY_FACTS admission (EXECUTION_STATUS = 'SUCCESS' assumed)
    if not (qtype in _ADMIT_TYPES
            or any(_like(qtype, p, ci=True) for p in ("%POLICY%", "%USER%", "%ROLE%", "DROP%", "TRUNCATE%"))):
        return False
    drop = _like(qtype, "DROP%", ci=True) or _like(qtype, "TRUNCATE%", ci=True)
    replace = qtype in ("CREATE_TABLE", "CREATE_TABLE_AS_SELECT") and _like(text, "%OR REPLACE%", ci=True)
    if drop:
        kind, base = "DESTRUCTIVE", 90
    elif qtype in ("GRANT", "REVOKE"):
        kind, base = "PRIVILEGE", 80
    elif _like(qtype, "%POLICY%", ci=True) or _like(qtype, "%USER%", ci=True):
        kind, base = "SECURITY POLICY", 85
    elif _like(qtype, "ALTER%", ci=True) or _like(qtype, "RENAME%", ci=True):
        kind, base = "ALTER", 55
    elif replace:                                   # V105: CREATE ... OR REPLACE -> DESTRUCTIVE
        kind, base = "DESTRUCTIVE", 55
    else:
        kind, base = "CREATE", 30
    score = min(100, base + (10 if role in _ADMINS else 0) + (10 if _like(db or "", "%PROD%", ci=True) else 0))
    preview = text[:200]
    # the view's CHANGE RISK arm WHERE (7-day window assumed satisfied)
    gate = re.search(r"AND RISK_SCORE >= (\d+)\n", _view(_V151))
    assert gate
    if score < int(gate.group(1)):
        return False
    # the V151 exclusion, with the keep lists parsed FROM the migration
    types, previews = _keep_lists(_view(_V151))
    scope = (role or "").upper().startswith("TF_") or ((db or "").upper() == "DBA_MAINT_DB"
                                                       and (schema or "").upper() == "PUBLIC")
    norm = re.sub(r"[ \t\n\r\f\v]+", " ", preview.upper()).lstrip(" ")
    keep = any(_like(qtype, p, ci=True) for p in types) or any(_like(norm, p, ci=False) for p in previews)
    return not (kind == "DESTRUCTIVE" and scope and not keep)


_TF = "TF_SFR_PRD_ADMIN"
_TF_ETL = "TF_SFR_PRD_ETL"


@pytest.mark.parametrize("row, surfaced", [
    # --- TF_* identity / policy drops: hidden by V088, surfaced by V151 -----------------------
    ((_TF, "DROP_USER", "ALFA_EDW_PRD", "PUBLIC", "DROP USER SVC_ETL"), True),
    ((_TF, "DROP_ROLE", None, None, 'drop role if exists "R_X"'), True),              # NULL session DB
    ((_TF, "DROP", "GOV", "POLICIES", "DROP MASKING POLICY GOV.POLICIES.PII"), True),
    ((_TF, "DROP", "GOV", "POLICIES", "\n  drop  row   access\npolicy GOV.P.RAP"), True),  # multi-line lower
    ((_TF, "DROP", None, None, "DROP DATABASE ROLE ALFA_EDW_PRD.DBR_READ"), True),
    ((_TF, "DROP", None, None, "DROP APPLICATION ROLE APP.AR_X"), True),
    ((_TF, "DROP", None, None, "DROP NETWORK POLICY NP_ALFA"), True),
    ((_TF, "DROP", None, None, "\tDROP BACKUP POLICY BP_DAILY"), True),                   # O-1 addition
    ((_TF, "DROP", None, None, "DROP STORAGE LIFECYCLE POLICY SLP_X"), True),
    (("DBA_HUMAN", "DROP_USER", "DBA_MAINT_DB", "PUBLIC", "DROP USER BOB"), True),         # app-scratch session
    # --- never hidden: human / unattributed drops, non-destructive TF_* rows ---------------
    (("DBA_HUMAN", "DROP", "ALFA_EDW_PRD", "DW", "DROP TABLE X"), True),
    ((None, "DROP", None, None, "DROP TABLE X"), True),
    ((_TF, "GRANT", "ALFA_EDW_PRD", "DW", "GRANT SELECT ON ALL TABLES IN SCHEMA DW TO ROLE R"), True),
    ((_TF, "ALTER_USER", None, None, "ALTER USER SVC SET DISABLED = TRUE"), True),
    # V105 CREATE ... OR REPLACE -> DESTRUCTIVE (base 55): only an admin role in a %PROD% session db
    # reaches 70 (55 + 10 + 10). Note the +10 keys on the literal PROD, so an _PRD db does not get it.
    (("ACCOUNTADMIN", "CREATE_TABLE", "EDW_PROD", "DW", "CREATE OR REPLACE TABLE DW.X (A INT)"), True),
    (("ACCOUNTADMIN", "CREATE_TABLE", "ALFA_EDW_PRD", "DW", "CREATE OR REPLACE TABLE DW.X (A INT)"), False),
    # --- TF_* ETL churn: stays excluded ---------------------------------------------------------
    ((_TF_ETL, "TRUNCATE_TABLE", "ALFA_EDW_PRD", "DW", "TRUNCATE TABLE ALFA_EDW_PRD.DW.FACT_POLICY"), False),
    ((_TF_ETL, "DROP", "ALFA_EDW_PRD", "DW", "DROP TABLE IF EXISTS DW.POLICY_STG"), False),
    ((_TF_ETL, "DROP", None, None, "DROP TABLE ALFA_EDW_PRD.DW.STG_X"), False),         # NULL-db flood
    ((_TF_ETL, "DROP", None, None, "DROP TABLE POLICY"), False),
    ((_TF_ETL, "DROP", "ALFA_EDW_PRD", "DW", "DROP TABLE DW.USER_ROLES"), False),
    ((_TF_ETL, "DROP", "ALFA_EDW_PRD", "DW", "DROP SCHEMA ALFA_EDW_PRD.STG_POLICY"), False),
    ((_TF, "DROP", None, None, "DROP TAG GOV.TAGS.PII"), False),                         # not in the keep list
    (("OVERWATCH_APP", "DROP", "DBA_MAINT_DB", "PUBLIC", "DROP TABLE DBA_MAINT_DB.PUBLIC.TMP_X"), False),
    # --- below the arm's RISK_SCORE >= 70 gate: never reaches the queue at all ------------------
    ((_TF_ETL, "CREATE_TABLE_AS_SELECT", "ALFA_EDW_PRD", "DW", "CREATE OR REPLACE TABLE DW.X AS SELECT 1"), False),
    (("ACCOUNTADMIN", "CREATE_TABLE", "DBA_MAINT_DB", "PUBLIC", "CREATE OR REPLACE TABLE T (A INT)"), False),
    (("DBA_HUMAN", "ALTER", "ALFA_EDW_PRD", "DW", "ALTER TABLE DW.X ADD COLUMN B INT"), False),
])
def test_v151_semantics_truth_table(row, surfaced):
    assert _surfaces(*row) is surfaced


def test_tf_create_or_replace_never_reaches_the_arm():
    """Why the exclusion need not special-case CREATE OR REPLACE: a TF_* role is never an admin
    role, so its DESTRUCTIVE CREATE OR REPLACE tops out at 55 + 10 (PROD db) = 65 < 70."""
    for db in ("EDW_PROD", "ALFA_EDW_PRD", "ALFA_EDW_DEV", None):
        assert not _surfaces(_TF_ETL, "CREATE_TABLE", db, "DW", "CREATE OR REPLACE TABLE X (A INT)")


def test_every_surfaced_identity_policy_drop_is_critical_and_moves_the_domain_score():
    """CHANGELOG disclosure: each surfaced row is CRITICAL (DROP base 90) with IMPACT_COUNT 1, so
    1 row in 7 days reads Watch (75) and 2 read Act (50)."""
    assert "WHEN RISK_SCORE >= 90 THEN 'CRITICAL'" in _latest_security_facts_body()[1]
    assert "           1, EVENT_TS::TIMESTAMP_NTZ," in _view(_V151)            # IMPACT_COUNT = 1
    cov = pd.DataFrame({"DOMAIN": ["CHANGE RISK"], "COVERAGE": ["COMPLETE"]})

    def change_risk(n: int):
        exc = pd.DataFrame({"DOMAIN": ["CHANGE RISK"] * n, "SEVERITY": ["CRITICAL"] * n,
                            "IMPACT_COUNT": [1] * n})
        return next(d for d in domain_posture(exc, cov) if d.domain == "CHANGE RISK")

    assert (change_risk(1).score, change_risk(1).state) == (75, "Watch")
    assert (change_risk(2).score, change_risk(2).state) == (50, "Act")


# ---------------------------------------------------------------------------------------------
# app mirror (the Security diagnostic) == the V151 literals
# ---------------------------------------------------------------------------------------------
def test_app_diagnostic_mirrors_the_v151_keep_lists():
    types, previews = _keep_lists(_view(_V151))
    assert types == security_sql.CHANGE_RISK_KEEP_QUERY_TYPES
    assert previews == security_sql.CHANGE_RISK_KEEP_PREVIEWS
    sql = security_sql.change_risk_destructive_breakdown(7)
    assert _keep_lists(sql) == (types, previews)
    assert "AS QUERY_TYPE" in sql and "AS DROP_CLASS" in sql
    assert "'identity / policy'" in sql and "'other object'" in sql and "'data object'" not in sql
    assert re.search(r"SUM\(COUNT_IF\(UPPER\(COALESCE\(ROLE_NAME, ''\)\) LIKE 'TF~_%' ESCAPE '~' AND \(.*?\)\)\) "
                     r"OVER \(\) AS TF_IDENTITY_POLICY_EVENTS", sql, re.S)
    assert "GROUP BY 1, 2, 3, 4, 5, 6" in sql
    # the TF_* test the diagnostic uses is byte-identical to the view's
    assert security_sql._TF_ROLE_SQL in _block(_view(_V151), _V151_BLOCK_START)


def test_v151_view_still_dropped_by_teardown():
    assert "V_SECURITY_EXCEPTION_QUEUE" in _read("snowflake/teardown.sql")


def test_v151_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    statements = list(_plain_statements(_V151))
    assert any(s.startswith("CREATE OR REPLACE VIEW") for s in statements)
    for statement in statements:
        sqlglot.parse(statement, dialect="snowflake")


# ---------------------------------------------------------------------------------------------
# shared lockstep, pinned to the wave-2a tip (V154). The integrator lands validate.sql, the docs
# and admin _EXPECTED_MIGRATIONS once for V151-V154, so these fail on the slice branch alone.
# ---------------------------------------------------------------------------------------------
def test_validate_floor_pins_the_wave_tip():
    val = _read("snowflake/validate.sql")
    assert "V001..V154 applied" in val and "VERSION BETWEEN 1 AND 154) = 154" in val


def test_docs_and_admin_track_v151():
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert _V151_NAME in _read(rel), rel
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 151 in _EXPECTED_MIGRATIONS
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_" not in _EXPECTED_MIGRATIONS[151]
