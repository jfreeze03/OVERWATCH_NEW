"""ETL reference-data gap monitor (Phase 1) — parser + scan builder.

Covers the config parser (entry separators, malformed rows), the UNION-ALL
MINUS scan, the dormant/empty paths, and injection fail-closed behavior.
"""

from __future__ import annotations

from app.data import etl_control_sql as etl

_XLAT = "ALFA_EDW_PRD.DB_V_PROD_BASE.TERADATA_ETL_REF_XLAT"
_STG = "ALFA_EDW_PRD.DB_T_PROD_STAG.PC_UWISSUETYPE"
_ONE = f"pc_uwissuetype.code | {_STG} | CODE_STG"


# --- parse_ref_gap_checks ---------------------------------------------------

def test_parse_single_check() -> None:
    checks, warns = etl.parse_ref_gap_checks(_ONE)
    assert warns == []
    assert len(checks) == 1
    c = checks[0]
    assert c.name == "pc_uwissuetype.code"
    assert c.staging_fqn == _STG
    assert c.staging_col == "CODE_STG"


def test_parse_multiple_entries_newline_and_semicolon() -> None:
    raw = f"{_ONE}\nother.code | DB.SCH.T2 | VAL_STG ; third.code | DB.SCH.T3 | C3"
    checks, warns = etl.parse_ref_gap_checks(raw)
    assert warns == []
    assert [c.name for c in checks] == ["pc_uwissuetype.code", "other.code", "third.code"]


def test_parse_skips_blank_and_comment_lines() -> None:
    raw = f"# a header comment\n\n{_ONE}\n   \n# trailing note"
    checks, warns = etl.parse_ref_gap_checks(raw)
    assert warns == []
    assert len(checks) == 1


def test_parse_malformed_entry_warns_not_raises() -> None:
    raw = f"{_ONE}\nbroken row without pipes\nmissing.col | DB.SCH.T |"
    checks, warns = etl.parse_ref_gap_checks(raw)
    assert len(checks) == 1  # only the valid one survives
    assert len(warns) == 2   # both malformed rows are reported
    assert all("malformed" in w.lower() for w in warns)


def test_parse_empty_is_empty() -> None:
    for raw in ("", "   ", None):
        checks, warns = etl.parse_ref_gap_checks(raw)
        assert checks == [] and warns == []


def test_parse_pin_marker_strips_star_and_flags() -> None:
    checks, warns = etl.parse_ref_gap_checks(f"*{_ONE}")
    assert warns == []
    assert checks[0].pinned is True
    assert checks[0].name == "pc_uwissuetype.code"  # the '*' is not part of the name


def test_parse_star_only_name_is_malformed() -> None:
    checks, warns = etl.parse_ref_gap_checks(f"* | {_STG} | CODE_STG")
    assert checks == []
    assert len(warns) == 1


def test_check_database_is_first_fqn_segment() -> None:
    check = etl.parse_ref_gap_checks(_ONE)[0][0]
    assert check.database == "ALFA_EDW_PRD"
    assert check.pinned is False


# --- filter_checks_by_database ----------------------------------------------

def _mixed_checks() -> list[etl.RefGapCheck]:
    raw = (
        f"*{_ONE}\n"                                         # pinned, ALFA_EDW_PRD
        "other.code | OTHER_DB.SCH.T2 | VAL_STG\n"           # OTHER_DB
        "third.code | ALFA_EDW_PRD.SCH.T3 | C3")             # ALFA_EDW_PRD
    return etl.parse_ref_gap_checks(raw)[0]


def test_filter_empty_database_keeps_all() -> None:
    checks = _mixed_checks()
    assert etl.filter_checks_by_database(checks, "") == checks
    assert etl.filter_checks_by_database(checks, "   ") == checks


def test_filter_scopes_to_database_but_keeps_pinned() -> None:
    checks = _mixed_checks()
    kept = etl.filter_checks_by_database(checks, "OTHER_DB")
    names = {c.name for c in kept}
    # pinned pc_uwissuetype survives despite being in a different database
    assert "pc_uwissuetype.code" in names
    assert "other.code" in names          # matches the scoped database
    assert "third.code" not in names      # different database, not pinned


def test_filter_is_case_insensitive() -> None:
    checks = _mixed_checks()
    kept = etl.filter_checks_by_database(checks, "alfa_edw_prd")
    names = {c.name for c in kept}
    assert names == {"pc_uwissuetype.code", "third.code"}


# --- reference_gap_scan -----------------------------------------------------

def test_scan_builds_minus_union_with_cap_and_order() -> None:
    checks, _ = etl.parse_ref_gap_checks(
        f"{_ONE}\nother.code | ALFA_EDW_PRD.DB_T_PROD_STAG.OTHER | VAL_STG")
    sql, errs = etl.reference_gap_scan(checks, _XLAT)
    assert errs == []
    # both checks present, joined by UNION ALL
    assert sql.count("UNION ALL") == 1
    assert sql.count("MINUS") == 2
    # the family name drives both the label literal and the XLAT filter
    assert "'pc_uwissuetype.code'" in sql
    assert f"{etl.XLAT_NAME_COL} = 'pc_uwissuetype.code'" in sql
    assert etl.XLAT_VALUE_COL in sql
    assert _STG in sql and _XLAT in sql
    # deterministic truncation: ORDER BY + LIMIT are IN the SQL, not left to pandas
    assert "ORDER BY CHECK_NAME, NEW_CODE" in sql
    assert f"LIMIT {etl.MAX_CODES}" in sql


def test_scan_matches_the_manual_morning_query_shape() -> None:
    # The proven manual check: staging code column MINUS the XLAT values for its family.
    checks, _ = etl.parse_ref_gap_checks(_ONE)
    sql, _ = etl.reference_gap_scan(checks, _XLAT)
    assert "SELECT s.CODE_STG AS NEW_CODE" in sql
    assert f"FROM {_STG} s WHERE s.CODE_STG IS NOT NULL" in sql
    assert f"SELECT x.{etl.XLAT_VALUE_COL} FROM {_XLAT} x" in sql


def test_scan_empty_when_unconfigured() -> None:
    checks, _ = etl.parse_ref_gap_checks(_ONE)
    # no reference table -> nothing to run
    assert etl.reference_gap_scan(checks, "") == ("", [])
    # no checks -> nothing to run
    assert etl.reference_gap_scan([], _XLAT) == ("", [])


def test_scan_drops_and_reports_unsafe_identifier() -> None:
    # A hostile staging FQN must fail closed: its check is dropped, not emitted.
    bad = etl.RefGapCheck(name="evil", staging_fqn="T; DROP TABLE X", staging_col="C")
    good = etl.parse_ref_gap_checks(_ONE)[0][0]
    sql, errs = etl.reference_gap_scan([good, bad], _XLAT)
    assert len(errs) == 1
    assert "evil" in errs[0]
    assert "DROP" not in sql.upper()  # the unsafe fragment never reaches the SQL
    assert "MINUS" in sql             # the good check still builds


def test_scan_all_unsafe_yields_no_sql() -> None:
    bad = etl.RefGapCheck(name="x", staging_fqn="a b c", staging_col="1col")
    sql, errs = etl.reference_gap_scan([bad], _XLAT)
    assert sql == ""
    assert len(errs) == 1
