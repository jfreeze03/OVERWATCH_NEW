"""ETL process-control readers — the customer's Informatica-orchestrated nightly cycle.

Phase 1: reference-data gap detection. The nightly load fails when a source
system emits a code that has no translation row in the reference (XLAT) table,
so this finds codes present in a *staging* table but MISSING from the reference
table — the manual morning ``MINUS`` check, generalized across the whole XLAT
code family and driven from SETTINGS config (adding a check is an Admin edit, not
a code change).

Config (Admin ▸ SETTINGS):
  ETL_REF_GAP_XLAT    the reference/translation table FQN, e.g.
                      ALFA_EDW_PRD.DB_V_PROD_BASE.TERADATA_ETL_REF_XLAT
  ETL_REF_GAP_CHECKS  one check per line (or ';'-separated); blank / '#' lines
                      are ignored:
                        <src_idntftn_nm> | <staging_fqn> | <staging_code_col>
                      e.g.
                        pc_uwissuetype.code | ALFA_EDW_PRD.DB_T_PROD_STAG.PC_UWISSUETYPE | CODE_STG

The reference table is matched on SRC_IDNTFTN_NM = <src_idntftn_nm>; its code
column is SRC_IDNTFTN_VAL (the Teradata ETL translation convention). Every
table / column is validated with safe_identifier, so a malformed config row
fails closed (its check is dropped and reported) rather than emitting unsafe
SQL. Pure module: bounded output, no Streamlit, no dollar rates.
"""

from __future__ import annotations

from dataclasses import dataclass

# The Teradata ETL translation (XLAT) table's own column convention. These are
# fixed for the reference table (the family axis is SRC_IDNTFTN_NM, the translated
# value is SRC_IDNTFTN_VAL); only the staging side varies per code type, which is
# why the staging table + column live in config while these stay constants.
XLAT_NAME_COL = "SRC_IDNTFTN_NM"
XLAT_VALUE_COL = "SRC_IDNTFTN_VAL"

MAX_CODES = 1000  # a real gap is a handful of codes; the cap only guards a misconfig


@dataclass(frozen=True)
class RefGapCheck:
    """One reference-gap check: staging code column vs the XLAT family member.

    ``name`` is the SRC_IDNTFTN_NM value (e.g. ``pc_uwissuetype.code``); it is
    both the label and the XLAT filter, so one config field drives both sides.
    ``pinned`` checks (config prefix ``*``) always show, ignoring the scope-bar
    Database filter — the operator's every-morning check stays visible whatever
    database is scoped. Every other check honors the Database filter, keyed on
    ``database`` (the first segment of its staging FQN).
    """

    name: str
    staging_fqn: str
    staging_col: str
    pinned: bool = False

    @property
    def database(self) -> str:
        """The staging table's database (first FQN segment), for the scope filter."""
        return self.staging_fqn.split(".", 1)[0].strip().upper()


def parse_ref_gap_checks(raw: object) -> tuple[list[RefGapCheck], list[str]]:
    """Parse the ETL_REF_GAP_CHECKS setting into checks + parse warnings.

    One check per entry, ``[*]<name> | <staging_fqn> | <staging_col>``. A leading
    ``*`` on the name pins the check (always shown, ignores the Database filter).
    Entries are separated by newlines OR ``;`` (so the config survives a
    single-line Admin text field); blank entries and those beginning with ``#``
    are ignored. A malformed entry (not exactly three non-empty fields) is skipped
    and named in the returned warnings — a bad row never silently drops a check
    with no signal.
    """
    text = str(raw or "").strip()
    if not text:
        return [], []
    checks: list[RefGapCheck] = []
    warnings: list[str] = []
    # ';' and newline are interchangeable entry separators.
    entries = [seg.strip() for seg in text.replace(";", "\n").splitlines()]
    for seg in entries:
        if not seg or seg.startswith("#"):
            continue
        parts = [p.strip() for p in seg.split("|")]
        if len(parts) != 3 or not all(parts):
            warnings.append(f"Ignored malformed check (need name | staging_table | code_column): {seg!r}")
            continue
        name, pinned = parts[0], False
        if name.startswith("*"):
            name, pinned = name[1:].strip(), True
        if not name:
            warnings.append(f"Ignored malformed check (empty name): {seg!r}")
            continue
        checks.append(RefGapCheck(name=name, staging_fqn=parts[1], staging_col=parts[2], pinned=pinned))
    return checks, warnings


def filter_checks_by_database(
    checks: list[RefGapCheck], database: str
) -> list[RefGapCheck]:
    """Keep checks in the scope-bar Database, plus every pinned check.

    ``database`` is the scope bar's exact database name; ``""`` (all) keeps every
    check. Pinned checks are always kept — the every-morning pc_uwissuetype check
    stays visible whatever database is scoped, per the owner's ask.
    """
    db = str(database or "").strip().upper()
    if not db:
        return list(checks)
    return [c for c in checks if c.pinned or c.database == db]


def _check_sql(check: RefGapCheck, xlat_fqn: str) -> str:
    """One check's gap subquery: staging codes MINUS the XLAT codes for its family.

    MINUS (not NOT IN) mirrors the operator's proven manual query and is NULL-safe
    — a NULL in the reference column can't swallow the whole result the way a
    NOT-IN subquery would. The outer TO_VARCHAR keeps NEW_CODE type-stable so the
    per-check subqueries UNION cleanly even when their code columns differ in type.
    Identifiers are validated (fail-closed); the family name is a quoted literal.
    """
    from app.core.sqlsafe import safe_identifier, sql_literal

    stg = safe_identifier(check.staging_fqn, allow_qualified=True)
    col = safe_identifier(check.staging_col)
    xlat = safe_identifier(xlat_fqn, allow_qualified=True)
    name_lit = sql_literal(check.name)
    return (
        f"SELECT {name_lit} AS CHECK_NAME, TO_VARCHAR(g.NEW_CODE) AS NEW_CODE FROM (\n"
        f"  SELECT s.{col} AS NEW_CODE FROM {stg} s WHERE s.{col} IS NOT NULL\n"
        f"  MINUS\n"
        f"  SELECT x.{XLAT_VALUE_COL} FROM {xlat} x WHERE x.{XLAT_NAME_COL} = {name_lit}\n"
        f") g"
    )


def reference_gap_scan(
    checks: list[RefGapCheck], xlat_fqn: str, *, max_codes: int = MAX_CODES
) -> tuple[str, list[str]]:
    """Build the UNION-ALL gap scan across all checks + per-check build errors.

    Returns ``(sql, errors)``. A check whose identifiers fail validation is
    dropped and named in ``errors`` (one bad table name never kills the whole
    scan). ``sql`` is ``""`` when no valid check survives OR the reference table
    is unset — the caller then renders a setup hint instead of running nothing.
    ORDER BY + LIMIT live IN the SQL so the row cap truncates deterministically.
    """
    if not str(xlat_fqn or "").strip() or not checks:
        return "", []
    parts: list[str] = []
    errors: list[str] = []
    for check in checks:
        try:
            parts.append(_check_sql(check, xlat_fqn))
        except ValueError as exc:
            errors.append(f"Check {check.name!r} skipped: {exc}")
    if not parts:
        return "", errors
    union = "\nUNION ALL\n".join(parts)
    sql = (
        "SELECT CHECK_NAME, NEW_CODE FROM (\n"
        f"{union}\n"
        f") ORDER BY CHECK_NAME, NEW_CODE\nLIMIT {int(max_codes)}"
    )
    return sql, errors
