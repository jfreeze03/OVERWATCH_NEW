"""Locks for V154 — incident [attach] arm + [auto-mitigate] sweep (Next-Fifty #12b).

SP_INCIDENT_AUTODECLARE is re-derived from V099 (its current definition; lineage V032 -> V098 ->
V099) with three anchored deltas: DECLARE +attached/mitigated/emsg, an exception-isolated [attach]
arm that links a later unlinked OPEN/ACK CRITICAL to the OPEN/MITIGATED incident its
family-already-open guard matched, and an exception-isolated [auto-mitigate] sweep that moves an
OPEN incident forward-only to MITIGATED once every ALERT member has been RESOLVED >= 1h (never
RESOLVED — closing stays human). Adds INCIDENTS.MITIGATED_BY (owner decision O-5). Byte-locked to
outputs/gen_v154.py; the normalize-and-compare lock proves nothing else in V099 moved (the round-13
class: a re-derivation silently dropping an intervening version's change).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIGDIR = _ROOT / "snowflake" / "migrations"
_MIG = (_MIGDIR / "V154__incident_attach_automitigate.sql").read_text(encoding="utf-8")
_V099 = (_MIGDIR / "V099__incident_autodeclare_company_scope.sql").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _proc(text: str) -> str:
    found = re.findall(
        r"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB\.OVERWATCH\.SP_INCIDENT_AUTODECLARE\(.*?\$\$;\n",
        text, re.S)
    assert len(found) == 1, len(found)
    return found[0]


_PROC = _proc(_MIG)
_ATTACH = _PROC.split("    -- [attach] V154", 1)[1].split("    -- [auto-mitigate] V154", 1)[0]
_MITIGATE = _PROC.split("    -- [auto-mitigate] V154", 1)[1].split("SELECT COUNT(*) INTO :made", 1)[0]


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def test_v154_regenerates_byte_identical(tmp_path):
    output = tmp_path / "regen.sql"
    result = subprocess.run(
        [sys.executable, str(_ROOT / "outputs" / "gen_v154.py")],
        env={**os.environ, "V154_OUT": str(output)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == _MIG, (
        "V154 drifted from its forward-generation — edit outputs/gen_v154.py, not the .sql."
    )


def test_v154_guarded_and_versioned():
    assert "not_ready EXCEPTION (-20154, 'V154 requires V153 first - apply migrations in order.');" in _MIG
    assert "IF (v < 153) THEN" in _MIG
    assert "SELECT 154 AS VERSION" in _MIG and "WHERE VERSION = 154)" in _MIG
    assert _MIG.startswith("-- V154__incident_attach_automitigate.sql\n")


def test_v154_file_order_guard_alter_marker_proc_version():
    guard = _MIG.index("EXCEPTION (-20154")
    alter = _MIG.index(
        "ALTER TABLE DBA_MAINT_DB.OVERWATCH.INCIDENTS ADD COLUMN IF NOT EXISTS MITIGATED_BY VARCHAR(200);")
    marker = _MIG.index("-- >>> derived:SP_INCIDENT_AUTODECLARE  (from V099; + [attach] arm + "
                        "[auto-mitigate] sweep, Next-Fifty #12b)")
    create = _MIG.index("CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.SP_INCIDENT_AUTODECLARE()")
    version = _MIG.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.SCHEMA_VERSION")
    assert guard < alter < marker < create < version


def test_v154_is_one_proc_one_additive_column_no_task_no_call():
    assert _MIG.count("CREATE OR REPLACE PROCEDURE") == 1
    assert _MIG.count("ALTER TABLE ") == 1
    for banned in ("CREATE TASK", "ALTER TASK", "CALL DBA_MAINT_DB", "CREATE OR REPLACE VIEW",
                   "CREATE OR REPLACE FUNCTION", "CREATE TABLE", "DROP ", "EXECUTE TASK",
                   "ALERT_CONFIG", "INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):
        assert banned not in _MIG, banned


def test_v154_normalizes_back_to_v099_byte_for_byte():
    """Strip exactly the three deltas; what remains must BE V099's proc (so V098's re-link guard,
    V099's company scope and every other byte survive the re-derivation)."""
    u = re.sub(r"\n    -- \[attach\].*?    END;\n(?=\n    SELECT COUNT\(\*\) INTO :made)", "", _PROC, flags=re.S)
    u = u.replace("    attached INT DEFAULT 0;\n    mitigated INT DEFAULT 0;\n    emsg VARCHAR;\n", "", 1)
    u = re.sub(r"    RETURN 'auto-declared ' \|\| :made \|\| ' incident\(s\); attached '[^\n]*\n",
               "    RETURN 'auto-declared ' || :made || ' incident(s)';\n", u)
    assert u == _proc(_V099)


def test_v154_keeps_the_intervening_v098_and_v099_changes():
    assert "          AND i.COMPANY = c.COMPANY\n" in _PROC                                   # V099
    assert _PROC.count("m2.MEMBER_KIND = 'ALERT' AND m2.REF_ID = e.EVENT_ID") == 2           # V098 + attach
    assert "RETURN 'auto-declare off';" in _PROC


def test_v154_both_arms_sit_after_the_toggle_and_before_the_count():
    toggle = _PROC.index("IF (UPPER(:enabled) <> 'TRUE') THEN")
    member_insert = _PROC.index("INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENT_MEMBERS")
    attach = _PROC.index("    -- [attach] V154")
    mitigate = _PROC.index("    -- [auto-mitigate] V154")
    count = _PROC.index("SELECT COUNT(*) INTO :made FROM _OW_AUTODECL;")
    assert toggle < member_insert < attach < mitigate < count
    # the header discloses the toggle dependency and the first-run MITIGATED batch
    head = _MIG.split("EXECUTE IMMEDIATE", 1)[0]
    assert "INCIDENT_AUTO_DECLARE_CRITICAL" in head and "'auto-declare off'" in head
    assert "FIRST RUN" in head and "Re-derives SP_INCIDENT_AUTODECLARE from V099" in head


def test_v154_attach_arm_is_the_guard_witness_set_one_target_per_event():
    a = _ATTACH
    assert "ON i.COMPANY = e.COMPANY" in a
    assert "AND i.STATUS IN ('OPEN', 'MITIGATED')" in a
    assert "AND m.MEMBER_KIND = 'ALERT'" in a
    assert ("SPLIT_PART(COALESCE(a.DEDUPE_KEY, a.EVENT_ID), '|', 1)\n"
            "                 = SPLIT_PART(COALESCE(e.DEDUPE_KEY, e.EVENT_ID), '|', 1)") in a
    assert "WHERE UPPER(e.SEVERITY) = 'CRITICAL'" in a
    assert "AND e.STATUS IN ('OPEN', 'ACK')" in a
    assert "AND e.RAISED_AT >= DATEADD('hour', -24, CURRENT_TIMESTAMP())" in a   # the crit CTE window
    assert "QUALIFY ROW_NUMBER() OVER (" in a and "PARTITION BY e.EVENT_ID" in a
    assert "i.DETECTED_AT DESC, i.INCIDENT_ID) = 1" in a
    assert "attached := SQLROWCOUNT;" in a
    # never creates an incident, never moves a STATUS
    assert "INSERT INTO DBA_MAINT_DB.OVERWATCH.INCIDENTS" not in a and "UPDATE " not in a


def test_v154_mitigate_sweep_is_forward_only_and_never_closes():
    m = _MITIGATE
    assert _PROC.count("SET STATUS = 'MITIGATED'") == 1
    assert "WHERE i.STATUS = 'OPEN'\n" in m and "AND i.STATUS = 'OPEN';" in m
    assert "SET STATUS = 'RESOLVED'" not in _PROC and "'RESOLVED'," not in m
    assert "HAVING COUNT_IF(e.EVENT_ID IS NULL OR e.STATUS <> 'RESOLVED') = 0" in m
    assert "AND MAX(e.RESOLVED_AT) <= DATEADD('hour', -1, CURRENT_TIMESTAMP());" in m   # 1h dwell
    assert "MITIGATED_AT = r.MITIGATED_TS" in m and "GREATEST(MAX(e.RESOLVED_AT), MAX(i.DETECTED_AT))" in m
    assert "MITIGATED_BY = 'SP_INCIDENT_AUTODECLARE'" in m
    assert "mitigated := SQLROWCOUNT;" in m
    # OWNER / ACK_AT stay human numbers: never in the sweep's SET
    set_clause = m.split("SET STATUS = 'MITIGATED',", 1)[1].split("FROM _OW_INC_MITIGATE r", 1)[0]
    assert "OWNER" not in set_clause and "ACK_AT" not in set_clause


def test_v154_each_arm_is_exception_isolated_with_its_own_error_type():
    for block, etype in ((_ATTACH, "incident_attach_failed"), (_MITIGATE, "incident_mitigate_failed")):
        assert block.count("    BEGIN\n") == 1 and "    EXCEPTION\n        WHEN OTHER THEN\n" in block
        assert "emsg := SQLERRM;" in block
        assert f"'IncidentAutodeclare', '{etype}', :emsg," in block
    assert "    attached INT DEFAULT 0;\n    mitigated INT DEFAULT 0;\n    emsg VARCHAR;\n" in _PROC
    assert ("RETURN 'auto-declared ' || :made || ' incident(s); attached ' || :attached"
            " || ' later critical(s); auto-mitigated ' || :mitigated || ' incident(s)';") in _PROC


def test_v154_ready_predicate_is_shared_with_the_app_reader():
    """The app's READY_TO_CLOSE (mart_sql._incident_ready_cte) is the sweep's HAVING minus the 1h
    dwell — the same member / successor tokens and the same live-successor subquery."""
    from app.data import mart_sql
    cte = _norm(mart_sql._incident_ready_cte())
    proc = _norm(_MITIGATE)
    for token in ("COUNT_IF(e.EVENT_ID IS NULL OR e.STATUS <> 'RESOLVED')",
                  "COUNT_IF(COALESCE(e.RESOLUTION_KIND, '') IN ('SUPERSEDED', 'SNOOZE_SUPPRESSED') "
                  "AND l.LAST_LIVE_AT >= e.RAISED_AT)",
                  "SELECT RULE_ID, COMPANY, MAX(RAISED_AT) AS LAST_LIVE_AT "
                  "FROM DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS WHERE STATUS IN ('OPEN', 'ACK', 'SNOOZED') "
                  "GROUP BY RULE_ID, COMPANY",
                  "ON l.RULE_ID = e.RULE_ID AND l.COMPANY = e.COMPANY",
                  "LEFT JOIN DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS e ON e.EVENT_ID = m.REF_ID"):
        assert token in proc, token
        assert token in cte, token
    assert "DATEADD('hour', -1" not in cte           # the app shows ready immediately; the sweep dwells


def test_v154_lineage_marker_names_the_true_previous_definer():
    from tests.test_proc_lineage import _lineage, _migrations
    rows = {(r["v"], r["proc"]): r for r in _lineage(_migrations())}
    row = rows[(154, "SP_INCIDENT_AUTODECLARE")]
    assert row["src"] == "marker" and row["claims"] == [99] and row["prev"] == 99


def test_v154_description_escapes_apostrophes_and_fits():
    m = re.search(r"SELECT 154 AS VERSION,\s*'((?:[^']|'')*)' AS DESCRIPTION", _MIG, re.S)
    assert m, "SCHEMA_VERSION description literal not found"
    raw = m.group(1)
    assert "''" in raw                                   # the family''s apostrophe is doubled
    assert re.search(r"(?<!')'(?!')", raw) is None       # no lone apostrophe ends the literal early
    assert len(raw.replace("''", "'")) <= 4000
    assert "re-derived from V099" in raw and "MITIGATED_BY" in raw


def test_v154_plain_sql_parses():
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_migrations_parse import _plain_statements
    for statement in _plain_statements(_MIG):
        sqlglot.parse(statement, dialect="snowflake")


# -- integration lockstep (validate floor / docs / Admin). Pinned to the WAVE TIP V154; these are
#    completed by the wave-2a integrator (snowflake/validate.sql, DEPLOYMENT.md, README.md and
#    admin._EXPECTED_MIGRATIONS are shared files a single slice does not edit).

def test_validate_and_docs_track_v154():
    val = _read("snowflake/validate.sql")
    assert "V001..V155 applied" in val and "VERSION BETWEEN 1 AND 155) = 155" in val
    for rel in ("DEPLOYMENT.md", "README.md"):
        assert "V154__incident_attach_automitigate.sql" in _read(rel), rel


def test_v154_in_expected_migrations():
    from app.ui.pages.admin import _EXPECTED_MIGRATIONS
    assert 154 in _EXPECTED_MIGRATIONS
