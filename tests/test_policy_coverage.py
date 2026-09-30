"""Next-Fifty #43 Phase 1: masking / row-access / projection / aggregation policy coverage (Security > Exposure).

Two toggle- and probe-gated reads of POLICY_REFERENCES (only the 10 columns the 2026-09-29 S0b/S1a/S1b probes
proved: S1b read seven, S1a read POLICY_NAME, S0b's column list shows POLICY_DB and POLICY_SCHEMA),
every distinct count keyed on a fully qualified name, the account totals read from SQL (never summed from the
per-database rows), a no-extra-scan ACCOUNT_POLICY_REFS on the Access network-policy read, and the owner defaults:
information only, out of the Decision-queue domain scores, the environment grouping is not a gap worklist.
Render tests use fakes (the tests/test_probe_read_honesty.py _FakeSt pattern); the builders' semantics run for real
in tests/test_policy_coverage_harness.py."""

from __future__ import annotations

import ast
import importlib
import re
from types import SimpleNamespace

import pandas as pd
import pytest

from tests._source import read

_PR = "SNOWFLAKE.ACCOUNT_USAGE.POLICY_REFERENCES"


def _pc():
    return importlib.import_module("app.logic.policy_coverage")


def _sql():
    return importlib.import_module("app.data.security_sql")


def _builders():
    s = _sql()
    return {"coverage": s.data_policy_coverage(), "parity": s.masking_environment_parity()}


# --------------------------------------------------------------------------------------------- SQL ----

def test_data_policy_coverage_contract():
    sqlglot = pytest.importorskip("sqlglot")
    s = _sql()
    sql = s.data_policy_coverage()
    sqlglot.parse_one(sql, read="snowflake")
    assert sql.count(_PR) == 1
    for part in (s._PR_ENTITY_FQN, s._PR_POLICY_FQN, s._PR_KIND, "FROM tot T", "LEFT JOIN by_db B ON 1 = 1"):
        assert part in sql, part
    assert "COMPANY_FOR_" not in sql                        # account-wide
    assert "LIMIT" not in sql                               # one row per masked database; run()'s cap applies
    # tag-based masking is split out by domain, and an unknown kind can never read as "no row-access policy"
    assert "REF_DOMAIN = 'TAG'" in sql and "REF_DOMAIN <> 'TAG'" in sql
    kinds = ", ".join(f"'{k}'" for k in (*s.DATA_POLICY_KINDS, "NETWORK_POLICY"))
    assert f"LISTAGG(DISTINCT IFF(PKIND IN ({kinds}), NULL, PKIND), ', ') AS OTHER_POLICY_KINDS" in sql


def test_data_policy_coverage_pins_the_keys_the_tag_split_and_the_status_count():
    """Review R1-19 / R1-20 / R1-24: the qualified-key projections, every TAG-sensitive expression and the
    not-ACTIVE count, pinned exactly (a substring anywhere in the SQL let an unqualified COLUMN_FQN, a dropped
    TAG filter or a constant 0 pass). tests/test_policy_coverage_harness.py runs the same SQL on rows."""
    s = _sql()
    sql = s.data_policy_coverage()
    for part in (
        f"{s._PR_ENTITY_FQN} AS ENTITY_FQN,",
        f"IFF(REF_COLUMN_NAME IS NULL, NULL, {s._PR_ENTITY_FQN} || '.' || REF_COLUMN_NAME) AS COLUMN_FQN,",
        f"{s._PR_POLICY_FQN} AS POLICY_FQN,",
        "UPPER(POLICY_STATUS) AS PSTATUS",
        "COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN <> 'TAG', COLUMN_FQN, NULL)) AS TOTAL_MASKED_COLUMNS",
        "COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN <> 'TAG', ENTITY_FQN, NULL)) AS TOTAL_MASKED_OBJECTS",
        "COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN <> 'TAG', REF_DATABASE_NAME, NULL)) "
        "AS TOTAL_MASKED_DATABASES",
        "COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN = 'TAG', ENTITY_FQN, NULL)) AS MASKING_TAGS",
        "COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN = 'TAG', REF_DATABASE_NAME, NULL)) "
        "AS MASKING_TAG_DATABASES",
        "COUNT(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND REF_DOMAIN = 'TAG', POLICY_FQN, NULL)) AS TAG_MASKING_POLICIES",
        "WHERE PKIND = 'MASKING_POLICY' AND REF_DOMAIN <> 'TAG' AND REF_DATABASE_NAME IS NOT NULL",
        "COUNT_IF(PKIND = 'MASKING_POLICY' AND PSTATUS <> 'ACTIVE') AS MASKING_REFS_NOT_ACTIVE",
        "LISTAGG(DISTINCT IFF(PKIND = 'MASKING_POLICY' AND PSTATUS <> 'ACTIVE', PSTATUS, NULL), ', ') "
        "AS NOT_ACTIVE_STATUSES",
        "COUNT_IF(PSTATUS <> 'ACTIVE') AS REFS_NOT_ACTIVE",
    ):
        assert part in sql, part
    assert sql.count("REF_DOMAIN <> 'TAG'") == 4        # the three column-level totals and the by_db filter
    assert sql.count("REF_DOMAIN = 'TAG'") == 3         # the three tag-line totals


def test_every_distinct_count_is_fully_qualified():
    s = _sql()
    b = _builders()
    counted = re.compile(r"COUNT\(DISTINCT (?:IFF\([^,]+, )?(\w+)")
    assert set(counted.findall(b["coverage"])) == {"ENTITY_FQN", "COLUMN_FQN", "POLICY_FQN", "REF_DATABASE_NAME"}
    # parity counts are grouped per (family, database, schema, object), so the column name is already qualified
    assert set(counted.findall(b["parity"])) == {"COL", "COLUMN_SET"}
    for sql in b.values():
        for bad in ("COUNT(DISTINCT POLICY_NAME", "COUNT(DISTINCT REF_ENTITY_NAME", "':' || REF_ENTITY_NAME"):
            assert bad not in sql, bad
    # the qualified keys carry database AND schema, never the bare name S1a counted (325 vs 1,255 tables)
    assert s._PR_ENTITY_FQN.startswith("COALESCE(REF_DATABASE_NAME, '')") and "REF_SCHEMA_NAME" in s._PR_ENTITY_FQN
    assert s._PR_POLICY_FQN.startswith("COALESCE(POLICY_DB, '')") and "POLICY_SCHEMA" in s._PR_POLICY_FQN


@pytest.mark.parametrize("col", ["REGION", "POLICY_ID", "REF_ARG_COLUMN_NAMES", "TAG_DATABASE", "TAG_SCHEMA",
                                 "TAG_NAME"])
def test_only_probe_proven_columns_are_read(col):
    for name, sql in _builders().items():
        assert re.search(rf"\b{col}\b", sql) is None, (name, col)


# POLICY_REFERENCES' 16 columns (S0b: N_COLS 16) and the 2026-09-29 probe that proved each one the #43 builders
# read (review R1-3: S1b read seven, S1a read POLICY_NAME, S0b's column list is the only proof of POLICY_DB and
# POLICY_SCHEMA).
_PROBE_PROVEN = {"POLICY_KIND": "S1b", "REF_ENTITY_DOMAIN": "S1b", "REF_DATABASE_NAME": "S1b",
                 "REF_SCHEMA_NAME": "S1b", "REF_ENTITY_NAME": "S1b", "REF_COLUMN_NAME": "S1b",
                 "POLICY_STATUS": "S1b", "POLICY_NAME": "S1a", "POLICY_DB": "S0b", "POLICY_SCHEMA": "S0b"}
_PR_ALL_COLUMNS = (*_PROBE_PROVEN, "REGION", "POLICY_ID", "REF_ARG_COLUMN_NAMES", "TAG_DATABASE", "TAG_SCHEMA",
                   "TAG_NAME")


def test_the_builders_read_exactly_the_probe_proven_columns():
    assert len(_PR_ALL_COLUMNS) == 16 and len(_PROBE_PROVEN) == 10
    b = _builders()
    read_cols = {c for c in _PR_ALL_COLUMNS if any(re.search(rf"\b{c}\b", sql) for sql in b.values())}
    assert read_cols == set(_PROBE_PROVEN)
    doc = _sql().data_policy_coverage.__doc__ or ""
    assert "S1b probe proved every column" not in doc
    assert "S1a read POLICY_NAME, and S0b's column list shows POLICY_DB and POLICY_SCHEMA" in " ".join(doc.split())


def test_builder_columns_match_the_logic_contract():
    sqlglot = pytest.importorskip("sqlglot")
    pc = _pc()
    b = _builders()
    assert sqlglot.parse_one(b["coverage"], read="snowflake").named_selects == [
        *pc.INVENTORY_COLUMNS, *pc.TOTAL_COLUMNS, *pc.TEXT_TOTAL_COLUMNS]
    assert sqlglot.parse_one(b["parity"], read="snowflake").named_selects == [
        *pc.PARITY_COLUMNS, *pc.PARITY_TOTAL_COLUMNS]


def test_masking_environment_parity_contract():
    sqlglot = pytest.importorskip("sqlglot")
    sql = _sql().masking_environment_parity()
    sqlglot.parse_one(sql, read="snowflake")
    assert sql.count(_PR) == 1
    for part in ("COUNT(*) OVER () AS TOTAL_NAMES",                          # uncapped totals, before the LIMIT
                 "SUM(IFF(PARITY = 'DIFFERS', 1, 0)) OVER () AS DIFFERING_NAMES",
                 "LIMIT 1000", "WHERE F.MASKED_FAMILY_DATABASES >= 2", "ORDER BY IFF(PARITY = 'DIFFERS', 0, 1)",
                 "UPPER(REF_ENTITY_DOMAIN) <> 'TAG'", "REF_COLUMN_NAME IS NOT NULL"):
        assert part in sql, part
    assert "COMPANY_FOR_" not in sql


def _mirror(sql_helper: str):
    """The Python twin of a #43 name helper, built from the regex literals the SQL itself carries."""
    lits = re.findall(r"'([^']*)'", sql_helper)
    return lits


def test_db_family_rule():
    s = _sql()
    fam_sql, env_sql = s._db_family("X"), s._db_env_suffix("X")
    assert fam_sql == "IFF(REGEXP_LIKE(X, '.+_[^_]+'), REGEXP_REPLACE(X, '_[^_]+$', ''), X)"
    assert env_sql == "IFF(REGEXP_LIKE(X, '.+_[^_]+'), REGEXP_SUBSTR(X, '[^_]+$'), NULL)"
    like, strip, _empty = _mirror(fam_sql)
    like2, suffix = _mirror(env_sql)
    assert like == like2

    def family(name: str) -> str:             # REGEXP_LIKE anchors the whole name, like re.fullmatch
        return re.sub(strip, "", name) if re.fullmatch(like, name) else name

    def env(name: str) -> str | None:
        m = re.search(suffix, name)
        return m.group(0) if re.fullmatch(like, name) and m else None

    cases = {"EDW_PRD": ("EDW", "PRD"), "ABC_METADATA_SIT": ("ABC_METADATA", "SIT"), "ADMIN": ("ADMIN", None),
             "USER$JOE": ("USER$JOE", None), "A_": ("A_", None)}
    for name, want in cases.items():
        assert (family(name), env(name)) == want, name
        assert _pc().db_family(name) == want[0], name                  # the page's mirror (unmasked siblings)
    # one source: the SQL helper is built from the logic layer's patterns
    pc = _pc()
    assert fam_sql == (f"IFF(REGEXP_LIKE(X, '{pc.FAMILY_NAME_PATTERN}'), "
                       f"REGEXP_REPLACE(X, '{pc.FAMILY_SUFFIX_PATTERN}', ''), X)")
    for name in ("ALFA_EDW_PRD", "ALFA_EDW_SAN", "TRXS_EDW_DEV", "DBA_MAINT_DB", "SNOWFLAKE", "_X", "A__B"):
        assert pc.db_family(name) == family(name), name
    # both builders share the one helper, and no environment or tenant name is hard-coded in either
    for sql in _builders().values():
        assert s._db_family("REF_DATABASE_NAME") in sql
        for word in ("'PRD'", "'PROD'", "'DEV'", "'SIT'", "'ALFA", "'TRXS", "'TREXIS"):
            assert word not in sql, word


def test_admin_network_policy_counts_account_domain_in_the_same_scan():
    sqlglot = pytest.importorskip("sqlglot")
    sql = _sql().admin_network_policy_coverage("ALFA")
    assert "COUNT_IF(REF_DOMAIN = 'ACCOUNT') AS ACCOUNT_POLICY_REFS" in sql
    select, group_by = sql.split("GROUP BY A.USER_NAME", 1)
    assert "T.ACCOUNT_POLICY_REFS" in select.split("FROM admins A", 1)[0]
    assert "T.ACCOUNT_POLICY_REFS" in group_by.split("\n", 1)[0]
    assert sql.count(_PR) == 1                                  # no extra scan
    assert "NP.REF_DOMAIN = 'USER'" in sql
    sqlglot.parse_one(sql, read="snowflake")


# ------------------------------------------------------------------------------------------- logic ----

def _cov_frame(rows: list[dict], **totals) -> pd.DataFrame:
    """A data_policy_coverage-shaped frame: ``rows`` are the per-database cells, ``totals`` the account columns
    (repeated on every row, as the SQL does)."""
    pc = _pc()
    base = dict.fromkeys(pc.TOTAL_COLUMNS, 0)
    base.update(dict.fromkeys(pc.TEXT_TOTAL_COLUMNS, ""))
    base.update(totals)
    out = []
    for r in rows:
        row = dict.fromkeys(pc.INVENTORY_COLUMNS)
        row.update(r)
        row.update(base)
        out.append(row)
    return pd.DataFrame(out, columns=[*pc.INVENTORY_COLUMNS, *pc.TOTAL_COLUMNS, *pc.TEXT_TOTAL_COLUMNS])


_SENTINEL = {"DATABASE_NAME": None}


def _probe_shape() -> pd.DataFrame:
    """The 2026-09-29 S1a/S1b shape: 2,416 masked columns on 1,255 tables in 5 databases, 15 masking tags in 4
    databases, no row-access / projection / aggregation policy, every status ACTIVE."""
    dbs = [("EDW_PRD", "PRD", 900, 1800, 4), ("EDW_DEV", "DEV", 200, 300, 3), ("EDW_SIT", "SIT", 100, 200, 3),
           ("MART_A", None, 50, 100, 2), ("OPS", None, 5, 16, 1)]
    rows = [{"DATABASE_NAME": d, "ENVIRONMENT": e, "MASKED_OBJECTS": o, "MASKED_COLUMNS": c, "MASKING_POLICIES": p,
             "REFS_NOT_ACTIVE": 0} for d, e, o, c, p in dbs]
    return _cov_frame(rows, TOTAL_MASKED_COLUMNS=2416, TOTAL_MASKED_OBJECTS=1255, TOTAL_MASKED_DATABASES=5,
                      TOTAL_MASKING_POLICIES=4, MASKING_TAGS=15, MASKING_TAG_DATABASES=4, TAG_MASKING_POLICIES=4)


_NO_ROW_LEVEL = ("No row-access, projection or aggregation policy is in use: the policy-reference view lists none "
                 "attached to any table or view.")
_TAG_LINE = ("Tag-based masking: 15 tags in 4 databases carry a masking policy (4 distinct policies). This counts "
             "the tags that carry a masking policy, once each even when a tag carries a policy for more than one "
             "data type; the columns each tag is set on are not listed here.")


def test_summary_reads_totals_never_sums():
    pc = _pc()
    frame = _cov_frame([
        {"DATABASE_NAME": "A_PRD", "MASKED_OBJECTS": 5, "MASKED_COLUMNS": 10, "MASKING_POLICIES": 3},
        {"DATABASE_NAME": "A_DEV", "MASKED_OBJECTS": 6, "MASKED_COLUMNS": 20, "MASKING_POLICIES": 3},
    ], TOTAL_MASKED_COLUMNS=25, TOTAL_MASKED_OBJECTS=9, TOTAL_MASKED_DATABASES=2, TOTAL_MASKING_POLICIES=4)
    cov = pc.summarize_policy_coverage(frame)
    assert cov is not None
    assert cov.masking_policies == 4                 # not 3 + 3: distinct policies are not additive
    assert cov.masked_columns == 25                  # TOTAL_MASKED_COLUMNS, not the 30 the rows add up to
    assert cov.masked_objects == 9 and cov.masked_databases == 2


def test_sentences_on_the_probe_shape():
    pc = _pc()
    cov = pc.summarize_policy_coverage(_probe_shape())
    assert cov is not None
    assert (cov.masked_columns, cov.masked_objects, cov.masked_databases, cov.masking_policies) == (2416, 1255, 5, 4)
    assert pc.tag_masking_sentence(cov) == _TAG_LINE
    assert pc.row_policy_sentences(cov) == (_NO_ROW_LEVEL,)
    assert pc.not_active_sentence(cov) == ""
    assert pc.other_kinds_sentence(cov) == ""


def test_row_policy_sentences_when_some_are_in_use():
    pc = _pc()
    cov = pc.summarize_policy_coverage(_cov_frame([_SENTINEL], ROW_ACCESS_OBJECTS=3, ROW_ACCESS_POLICIES=1))
    assert pc.row_policy_sentences(cov) == (
        "Row-access policies are attached to 3 tables or views (1 distinct policy).",
        "No projection or aggregation policy is in use: the policy-reference view lists none attached to any "
        "table or view.")
    cov = pc.summarize_policy_coverage(_cov_frame(
        [_SENTINEL], ROW_ACCESS_OBJECTS=1, ROW_ACCESS_POLICIES=2, PROJECTION_OBJECTS=4, PROJECTION_POLICIES=1,
        AGGREGATION_OBJECTS=2, AGGREGATION_POLICIES=2))
    assert pc.row_policy_sentences(cov) == (
        "Row-access policies are attached to 1 table or view (2 distinct policies).",
        "Projection policies are attached to 4 tables or views (1 distinct policy).",
        "Aggregation policies are attached to 2 tables or views (2 distinct policies).")


def test_tag_sentence_singular_and_none():
    pc = _pc()
    one = pc.summarize_policy_coverage(_cov_frame([_SENTINEL], MASKING_TAGS=1, MASKING_TAG_DATABASES=1,
                                                  TAG_MASKING_POLICIES=1))
    assert pc.tag_masking_sentence(one) == (
        "Tag-based masking: 1 tag in 1 database carries a masking policy (1 distinct policy). This counts the tags "
        "that carry a masking policy, once each even when a tag carries a policy for more than one data type; the "
        "columns each tag is set on are not listed here.")
    # review R1-2: MASKING_TAGS is COUNT(DISTINCT tag), not the references nor the objects a tag is set on
    # (tests/test_policy_coverage_harness.py: one tag carrying two policies counts once)
    for cov in (one, pc.summarize_policy_coverage(_probe_shape())):
        sentence = pc.tag_masking_sentence(cov)
        assert "assignment" not in sentence and "reference" not in sentence
    none = pc.summarize_policy_coverage(_cov_frame([_SENTINEL]))
    assert pc.tag_masking_sentence(none) == (
        "Tag-based masking: none. The policy-reference view lists no masking policy attached to a tag.")


def test_not_active_and_other_kinds_sentences():
    pc = _pc()
    one = pc.summarize_policy_coverage(_cov_frame([_SENTINEL], MASKING_REFS_NOT_ACTIVE=1,
                                                  NOT_ACTIVE_STATUSES="INACTIVE"))
    assert pc.not_active_sentence(one) == "1 masking reference reports a status other than ACTIVE: INACTIVE."
    many = pc.summarize_policy_coverage(_cov_frame([_SENTINEL], MASKING_REFS_NOT_ACTIVE=1200,
                                                   NOT_ACTIVE_STATUSES=None))
    assert pc.not_active_sentence(many) == ("1,200 masking references report a status other than ACTIVE: "
                                            "status not reported.")
    kinds = pc.summarize_policy_coverage(_cov_frame([_SENTINEL], OTHER_POLICY_KINDS=" JOIN_POLICY, ROW ACCESS "))
    assert pc.other_kinds_sentence(kinds) == ("Other policy kinds in the view, not covered here: "
                                              "JOIN_POLICY, ROW ACCESS.")
    # LISTAGG over no rows may come back '' or NULL: both read as "none"
    for empty in ("", None, float("nan")):
        cov = pc.summarize_policy_coverage(_cov_frame([_SENTINEL], OTHER_POLICY_KINDS=empty))
        assert pc.other_kinds_sentence(cov) == ""


def test_database_inventory_drops_the_sentinel_row():
    pc = _pc()
    empty = pc.database_inventory(_cov_frame([_SENTINEL]))
    assert empty.empty and list(empty.columns) == list(pc.INVENTORY_COLUMNS)
    assert list(pc.database_inventory(None).columns) == list(pc.INVENTORY_COLUMNS)
    inv = pc.database_inventory(_probe_shape())
    assert list(inv.columns) == list(pc.INVENTORY_COLUMNS)           # totals dropped: the CSV is this frame
    assert list(inv["DATABASE_NAME"]) == ["EDW_PRD", "EDW_DEV", "EDW_SIT", "MART_A", "OPS"]
    assert list(inv.index) == list(range(5))


def test_summary_is_none_without_the_total_columns():
    pc = _pc()
    assert pc.summarize_policy_coverage(None) is None
    assert pc.summarize_policy_coverage(pd.DataFrame()) is None
    assert pc.summarize_policy_coverage(_probe_shape().drop(columns=["MASKING_TAGS"])) is None
    assert pc.summarize_policy_coverage(_probe_shape().drop(columns=["OTHER_POLICY_KINDS"])) is None


def _parity_frame(n: int, total: int, differing: int) -> pd.DataFrame:
    pc = _pc()
    rows = [{"DATABASE_FAMILY": "EDW", "SCHEMA_NAME": "S", "OBJECT_NAME": f"T{i}",
             "PARITY": "DIFFERS" if i < differing else "SAME", "DATABASES_MASKED": 2, "MASKED_FAMILY_DATABASES": 3,
             "COLUMN_SETS": 1, "MASKED_IN": "EDW_DEV (2), EDW_PRD (2)", "NO_MASKING_REF_IN": "EDW_SIT",
             "TOTAL_NAMES": total, "DIFFERING_NAMES": differing} for i in range(n)]
    return pd.DataFrame(rows, columns=[*pc.PARITY_COLUMNS, *pc.PARITY_TOTAL_COLUMNS])


def test_parity_view_and_counts():
    pc = _pc()
    frame = _parity_frame(3, total=1500, differing=1)
    view = pc.parity_view(frame)
    assert list(view.columns) == list(pc.PARITY_COLUMNS)             # window totals are not display columns
    assert pc.parity_counts(frame) == (1500, 1)                       # from TOTAL_NAMES, not len(frame)
    assert pc.parity_summary_sentence(1500, 1) == (
        "1,500 masked table or view names belong to a database family with two or more masked databases; 1 is "
        "not masked the same way in every one of them. Databases with no column-level masking reference are not "
        "part of this grouping.")
    assert pc.parity_summary_sentence(1, 0) == (
        "1 masked table or view name belongs to a database family with two or more masked databases; 0 are not "
        "masked the same way in every one of them. Databases with no column-level masking reference are not part "
        "of this grouping.")


def test_parity_wording_says_unmasked_databases_are_left_out():
    """Review R1-1: the grouping only sees databases with a masked column; its column name, legend, summary and
    empty state say so (an unmasked environment is listed from SHOW DATABASES instead)."""
    pc = _pc()
    assert "MASKED_FAMILY_DATABASES" in pc.PARITY_COLUMNS and "FAMILY_DATABASES" not in pc.PARITY_COLUMNS
    assert "a database with no column-level masking reference appears in neither table" in pc.PARITY_LEGEND
    assert "A family here is the databases with masked columns" in pc.PARITY_LEGEND
    assert "MASKED_FAMILY_DATABASES counts the family's databases with masked columns" in pc.PARITY_LEGEND
    assert pc.PARITY_NOTHING_TO_GROUP == (
        "No two databases with masked columns share a name up to their last underscore, so there is nothing to "
        "group. Databases with no column-level masking reference are not part of this grouping.")


# The 2026-09-29 shape: ALFA_EDW_* has 7 databases (app/companies.py) but only some carry column masking.
_SHOW_NAMES = ["ALFA_EDW_PRD", "ALFA_EDW_DEV", "ALFA_EDW_SIT", "ALFA_EDW_SAN", "ALFA_EDW_PHX", "ALFA_EDW_SEA",
               "ALFA_EDW_MGM", "MART_A", "MART_B", "OPS", "OPS_ARCHIVE", "DBA_MAINT_DB", "SNOWFLAKE"]


def test_unmasked_family_databases():
    pc = _pc()
    got = pc.unmasked_family_databases(["ALFA_EDW_PRD", "ALFA_EDW_DEV", "ALFA_EDW_SIT", "MART_A", "OPS"],
                                       _SHOW_NAMES)
    assert got == (pc.FamilySiblings("ALFA_EDW", ("ALFA_EDW_MGM", "ALFA_EDW_PHX", "ALFA_EDW_SAN", "ALFA_EDW_SEA")),
                   pc.FamilySiblings("MART", ("MART_B",)),
                   pc.FamilySiblings("OPS", ("OPS_ARCHIVE",)))
    # only production masked: the grouping has nothing to group, the sibling list still names every environment
    (only_prd,) = pc.unmasked_family_databases(["ALFA_EDW_PRD"], _SHOW_NAMES)
    assert only_prd.family == "ALFA_EDW" and len(only_prd.unmasked) == 6 and "ALFA_EDW_PRD" not in only_prd.unmasked
    # every family member masked, a masked database SHOW does not list, and blank / NaN names: nothing to list
    assert pc.unmasked_family_databases(["MART_A", "MART_B"], ["MART_A", "MART_B", None, float("nan"), ""]) == ()
    assert pc.unmasked_family_databases([], _SHOW_NAMES) == ()


def test_listed_database_names():
    pc = _pc()
    assert pc.listed_database_names(pd.DataFrame({"created_on": [1, 2], "name": ["B", "A"]})) == ["B", "A"]
    assert pc.listed_database_names(pd.DataFrame({"NAME": [" X ", None, "", float("nan")]})) == ["X"]
    for frame in (None, pd.DataFrame(), pd.DataFrame({"created_on": [1]})):
        assert pc.listed_database_names(frame) == []


def test_sibling_lines():
    pc = _pc()
    sibs = pc.unmasked_family_databases(["ALFA_EDW_PRD", "MART_A"], _SHOW_NAMES)
    assert pc.sibling_lines(sibs) == (
        pc.SIBLINGS_LEAD,
        "ALFA_EDW: no column-level masking reference in ALFA_EDW_DEV, ALFA_EDW_MGM, ALFA_EDW_PHX, ALFA_EDW_SAN, "
        "ALFA_EDW_SEA and ALFA_EDW_SIT.",
        "MART: no column-level masking reference in MART_B.",
        pc.SIBLINGS_LAG)
    assert pc.sibling_lines(()) == (pc.SIBLINGS_NONE, pc.SIBLINGS_LAG)
    assert pc.sibling_lines((), listed_capped=True) == (pc.SIBLINGS_NONE, pc.SIBLINGS_LAG, pc.SIBLINGS_CAPPED)
    many = (pc.FamilySiblings("X", tuple(f"X_{i:02d}" for i in range(pc.SIBLING_NAMES_CAP + 3))),)
    line = pc.sibling_lines(many)[1]
    assert line.endswith(f"X_{pc.SIBLING_NAMES_CAP - 1:02d} and 3 more.") and f"X_{pc.SIBLING_NAMES_CAP:02d}" not in line
    # the unchecked states never claim there are none
    for text in (pc.SIBLINGS_UNCHECKED, pc.SIBLINGS_NO_NAMES):
        assert "were not checked" in text and "lists no" not in text


def test_sibling_lines_never_say_no_masked_column():
    """Review R2-1: the lines know only that a database has no COLUMN-LEVEL masking reference. Tag-based masking is
    not traced to columns, so with masking tags on the account a qualifier follows a non-empty list; the policy
    view's lag (and the cached database list) is stated on every rendered list; SHOW DATABASES is never 'live'."""
    pc = _pc()
    sibs = pc.unmasked_family_databases(["ALFA_EDW_PRD"], _SHOW_NAMES)
    tagged = pc.sibling_lines(sibs, tag_masking=True, listed_capped=True)
    assert tagged == (*pc.sibling_lines(sibs)[:-1], pc.SIBLINGS_TAG_QUALIFIER, pc.SIBLINGS_LAG, pc.SIBLINGS_CAPPED)
    assert pc.SIBLINGS_TAG_QUALIFIER == (
        "Tag-based masking is not traced to columns here, so a database masked only through a tag (or a tag set on "
        "the database or schema) can be listed.")
    assert pc.SIBLINGS_TAG_QUALIFIER not in pc.sibling_lines(sibs)                    # no tags: no qualifier
    assert pc.sibling_lines((), tag_masking=True) == (pc.SIBLINGS_NONE, pc.SIBLINGS_LAG)  # nothing listed to qualify
    assert "can lag up to 2h" in pc.SIBLINGS_LAG and "cached SHOW DATABASES read" in pc.SIBLINGS_LAG
    assert "cached for up to 1h" in pc.SIBLINGS_LAG and "in the last 3h can be listed" in pc.SIBLINGS_LAG   # R3-5
    texts = (*tagged, *pc.sibling_lines(()), pc.SIBLINGS_UNCHECKED, pc.SIBLINGS_NO_NAMES, pc.PARITY_LEGEND,
             pc.PARITY_NOTHING_TO_GROUP, pc.parity_summary_sentence(3, 1))
    for text in texts:
        assert "no masked column" not in text and not re.search(r"\blive\b", text, re.IGNORECASE), text


def test_the_lag_lines_count_the_panels_own_cache(monkeypatch):
    """Review r3 R3-5: the unmasked-sibling lines compare SHOW DATABASES against the masked databases of the
    coverage read, which is cached on the 'hourly' tier, and the lag line gave only the view's 2 hours. A database
    masked (or cloned from a masked one) 2.5h ago can be in the view but not in a read cached 1.9h after the
    masking, so it is still listed: the bound is the view's lag plus the cache, 3h. NO_MASKING had the same 2-hour
    window. Both lines carry the combined bound, humanized, from constants pinned to the reads' tier."""
    from app.core.query import CACHE_TTLS

    pc = _pc()
    assert pc.POLICY_VIEW_LAG_SEC == 7200
    assert pc.POLICY_READ_CACHE_SEC == CACHE_TTLS["hourly"] == 3600
    _fake, seen = _render_panel(monkeypatch, _ok(_probe_shape()))
    ((key, _sql_text, kw),) = seen["runs"]
    assert key == "sec_policy_cov" and CACHE_TTLS[kw["tier"]] == pc.POLICY_READ_CACHE_SEC
    _fake, seen = _render_parity(monkeypatch, _ok(_parity_frame(3, total=3, differing=1)))
    ((key, _sql_text, kw), _dbs) = seen["runs"]
    assert key == "sec_policy_parity" and CACHE_TTLS[kw["tier"]] == pc.POLICY_READ_CACHE_SEC
    # the reviewers' case: masked at t, the read cached at t + 1.9h (before the view caught up), listed at t + 2.5h
    masked_ago = 9000
    assert pc.POLICY_VIEW_LAG_SEC < masked_ago <= pc.POLICY_VIEW_LAG_SEC + pc.POLICY_READ_CACHE_SEC
    assert pc.SIBLINGS_LAG == (
        "The policy-reference view can lag up to 2h and this panel's reads of it are cached for up to 1h (Refresh "
        "data clears the cache), so a database masked or cloned in the last 3h can be listed; the database list is a "
        "cached SHOW DATABASES read, so a database created since that read is not listed.")
    assert pc.NO_MASKING == (
        "The policy-reference view lists no masking policy on any column or tag in this account, so there is no "
        "masking inventory. A policy attached in the last 3h may not be listed yet (the view can lag up to 2h and "
        "this read of it is cached for up to 1h; Refresh data clears the cache).")
    for text in (pc.SIBLINGS_LAG, pc.NO_MASKING):
        assert "2 hours" not in text and "in that time" not in text


def test_network_policy_caption_variants():
    pc = _pc()
    integration = ("A security integration's own network policy takes precedence over both when an admin signs "
                   "in through that integration.")
    assert pc.network_policy_caption(None) == (
        "A user-level policy overrides the account policy and pins an admin to known networks; an admin without "
        "one still falls under the account-level policy if one is set. " + integration)
    assert pc.network_policy_caption(1) == (
        "An account-level network policy is set (the policy-reference view lists it). A user-level policy takes "
        "precedence over it and pins an admin to known networks; an admin without one falls under the "
        "account-level policy. " + integration)
    assert pc.network_policy_caption(0) == (
        "The policy-reference view lists no account-level network policy (it can lag up to 2 hours); confirm with "
        "SHOW PARAMETERS LIKE 'NETWORK_POLICY' IN ACCOUNT. Without one, an admin with no user-level policy is "
        "limited only by a security integration's own network policy, when signing in through it.")
    # Snowflake's documented precedence (most specific wins): integration > user > account. No caption may say a
    # user-level policy outranks an integration's.
    for refs in (None, 0, 1):
        assert "integration's own network policy" in pc.network_policy_caption(refs)
    assert "applies to an admin without a user-level policy unless they sign in through a security integration" \
        in pc.ACCOUNT_POLICY_HINT
    assert pc.account_network_policy_refs(None) is None
    assert pc.account_network_policy_refs(pd.DataFrame({"USER_POLICY_REFS": [1]})) is None
    assert pc.account_network_policy_refs(pd.DataFrame({"ACCOUNT_POLICY_REFS": [None, None]})) is None
    assert pc.account_network_policy_refs(pd.DataFrame({"ACCOUNT_POLICY_REFS": [1, 1]})) == 1
    assert pc.account_network_policy_refs(pd.DataFrame({"ACCOUNT_POLICY_REFS": ["0"]})) == 0


def test_policy_coverage_logic_is_pure():
    src = read("app/logic/policy_coverage.py")
    imports = [ast.unparse(n) for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import | ast.ImportFrom)]
    assert not [i for i in imports if "app.data" in i or "app.ui" in i or "streamlit" in i], imports
    assert "datetime" not in src and "time.time" not in src
    assert "domain_posture" not in "\n".join(imports)


# ------------------------------------------------------------------------------------------ render ----

def _ok(df: pd.DataFrame):
    return SimpleNamespace(ok=True, empty=df.empty, df=df, error="", error_kind="", usable=lambda: not df.empty)


def _failed(kind: str):
    return SimpleNamespace(ok=False, empty=True, df=pd.DataFrame(), error=f"boom ({kind})", error_kind=kind,
                           usable=lambda: False)


class _FakeSt:
    def __init__(self, toggles_off: tuple[str, ...] = ()):
        self.calls: list[tuple[str, str]] = []
        self.session_state: dict = {}
        self._off = toggles_off
        self.toggles: list[str] = []

    def caption(self, text, *_a, **_k):
        self.calls.append(("caption", str(text)))

    def markdown(self, text, *_a, **_k):
        self.calls.append(("markdown", str(text)))

    def toggle(self, *_a, key: str = "", **_k):
        self.toggles.append(key)
        return key not in self._off

    def divider(self):
        self.calls.append(("divider", ""))

    def text(self, kind: str) -> str:
        return "\n".join(t for k, t in self.calls if k == kind)


def _patch(monkeypatch, results: dict, toggles_off: tuple[str, ...] = ()):
    from app.ui.pages import security as sec
    fake = _FakeSt(toggles_off)
    seen: dict = {"runs": [], "empty": [], "detail": [], "hint": [], "kpis": [], "tables": []}

    def fake_run(sql, *_a, key: str = "", **kw):
        seen["runs"].append((key, sql, kw))
        return results[key]

    def fake_empty(kind, msg, *_a, **k):
        seen["empty"].append((kind, msg))
        seen["detail"].append(k.get("detail"))
        seen["hint"].append(k.get("hint"))

    monkeypatch.setattr(sec, "st", fake)
    monkeypatch.setattr(sec, "run", fake_run)
    monkeypatch.setattr(sec, "section_header", lambda *_a, **_k: None)
    monkeypatch.setattr(sec, "result_caption", lambda *_a, **_k: None)
    monkeypatch.setattr(sec, "kpi_row", lambda items, *_a, **_k: seen["kpis"].append(items))
    monkeypatch.setattr(sec, "styled_table", lambda df, *_a, **k: seen["tables"].append((df, k)))
    monkeypatch.setattr(sec, "empty_state", fake_empty)
    return sec, fake, seen


def _render_panel(monkeypatch, result, *, toggles_off=("sec_policy_parity_toggle",)):
    sec, fake, seen = _patch(monkeypatch, {"sec_policy_cov": result}, toggles_off)
    sec._render_policy_coverage()
    return fake, seen


def test_policy_panel_is_off_until_toggled(monkeypatch):
    fake, seen = _render_panel(monkeypatch, _ok(_probe_shape()), toggles_off=("sec_policy_cov_toggle",))
    assert seen["runs"] == [] and seen["kpis"] == [] and seen["tables"] == [] and seen["empty"] == []
    assert fake.toggles == ["sec_policy_cov_toggle"]
    assert "not counted in the Decision-queue domain scores" in fake.text("caption")


# v4.605 review r3: an "Insufficient privileges" error ('privilege') is a setup absence like guard()'s
@pytest.mark.parametrize("kind", ["absent", "privilege", "unknown_function"])
def test_policy_panel_absent_view_is_setup(monkeypatch, kind):
    pc = _pc()
    fake, seen = _render_panel(monkeypatch, _failed(kind))
    assert seen["empty"] == [("needs_setup", pc.POLICY_VIEW_UNREADABLE)]
    assert not seen["kpis"] and not seen["tables"] and not fake.text("markdown")


@pytest.mark.parametrize("kind", ["missing_column", "timeout", "other"])
def test_policy_panel_other_failures_are_unavailable_with_the_error(monkeypatch, kind):
    fake, seen = _render_panel(monkeypatch, _failed(kind))
    ((state, msg),) = seen["empty"]
    assert state == "unavailable", kind
    assert seen["detail"] == [f"boom ({kind})"]
    assert "could not be read" in msg
    assert not seen["kpis"] and not seen["tables"] and not fake.text("markdown")


def test_policy_panel_renders_the_probe_shape(monkeypatch):
    pc = _pc()
    fake, seen = _render_panel(monkeypatch, _ok(_probe_shape()))
    ((key, sql, kw),) = seen["runs"]
    assert key == "sec_policy_cov" and sql == _sql().data_policy_coverage()
    assert kw["probe"] is True and kw["tier"] == "hourly"
    assert "ACCOUNT_USAGE" not in kw["source"] and "POLICY_REFERENCES" in kw["source"]
    ((kpis),) = seen["kpis"]
    assert [k["value"] for k in kpis] == ["2,416", "1,255", "5", "4"]
    md = fake.text("markdown")
    assert _TAG_LINE in md and _NO_ROW_LEVEL in md
    ((table, kwargs),) = seen["tables"]
    assert list(table.columns) == list(pc.INVENTORY_COLUMNS) and len(table) == 5
    assert kwargs["slug"] == "masking-by-database"
    assert pc.INVENTORY_NOTE in fake.text("caption")
    assert seen["empty"] == []
    assert "status other than ACTIVE" not in fake.text("caption")
    assert "Other policy kinds" not in fake.text("caption")
    assert fake.toggles == ["sec_policy_cov_toggle", "sec_policy_parity_toggle"]   # parity offered, still off


def test_policy_panel_no_masking_is_setup_never_clean(monkeypatch):
    pc = _pc()
    fake, seen = _render_panel(monkeypatch, _ok(_cov_frame([_SENTINEL])))
    assert seen["empty"] == [("needs_setup", pc.NO_MASKING)]
    assert all(state != "clean" for state, _ in seen["empty"])
    assert _NO_ROW_LEVEL in fake.text("markdown")                     # the row-level fact still renders
    assert "Tag-based masking" not in fake.text("markdown")
    assert not seen["tables"] and not seen["kpis"]
    assert fake.toggles == ["sec_policy_cov_toggle"]                  # no grouping offered without column masking


def test_policy_panel_tag_only_account(monkeypatch):
    pc = _pc()
    frame = _cov_frame([_SENTINEL], MASKING_TAGS=15, MASKING_TAG_DATABASES=4, TAG_MASKING_POLICIES=4,
                       TOTAL_MASKING_POLICIES=4)
    fake, seen = _render_panel(monkeypatch, _ok(frame))
    assert seen["empty"] == [("no_data_yet", pc.TAG_ONLY_MASKING)]
    md = fake.text("markdown")
    assert _TAG_LINE in md and _NO_ROW_LEVEL in md
    assert not seen["tables"] and not seen["kpis"]


def test_policy_panel_zero_rows_and_bad_shape_are_never_clean(monkeypatch):
    _, seen = _render_panel(monkeypatch, _ok(pd.DataFrame()))
    assert seen["empty"] == [("no_data_yet", "The policy-reference read returned no summary row, so coverage "
                                             "cannot be shown.")]
    _, seen = _render_panel(monkeypatch, _ok(_probe_shape().drop(columns=["ROW_ACCESS_OBJECTS"])))
    ((state, msg),) = seen["empty"]
    assert state == "unavailable" and "expected summary columns" in msg
    assert not seen["kpis"] and not seen["tables"]


def test_policy_panel_surfaces_not_active_and_other_kinds(monkeypatch):
    frame = _probe_shape()
    frame["MASKING_REFS_NOT_ACTIVE"] = 2
    frame["NOT_ACTIVE_STATUSES"] = "INACTIVE"
    frame["OTHER_POLICY_KINDS"] = "JOIN_POLICY"
    fake, _ = _render_panel(monkeypatch, _ok(frame))
    caps = fake.text("caption")
    assert "2 masking references report a status other than ACTIVE: INACTIVE." in caps
    assert "Other policy kinds in the view, not covered here: JOIN_POLICY." in caps


def _show_dbs(names=_SHOW_NAMES):
    return _ok(pd.DataFrame({"created_on": ["x"] * len(names), "name": names}))


def _render_parity(monkeypatch, result, dbs=None, masked=("ALFA_EDW_PRD", "ALFA_EDW_DEV", "ALFA_EDW_SIT")):
    sec, fake, seen = _patch(monkeypatch, {"sec_policy_parity": result,
                                           "sec_policy_parity_dbs": dbs if dbs is not None else _show_dbs()})
    sec._render_masking_parity(masked)
    return fake, seen


def test_parity_toggle_states(monkeypatch):
    pc = _pc()
    sec, fake, seen = _patch(monkeypatch, {}, toggles_off=("sec_policy_parity_toggle",))
    sec._render_masking_parity()
    assert seen["runs"] == []                                          # off until toggled

    for kind in ("absent", "privilege"):                              # review r3: privilege is a setup absence
        _, seen = _render_parity(monkeypatch, _failed(kind))
        assert seen["empty"] == [("needs_setup", pc.POLICY_VIEW_UNREADABLE)], kind

    _, seen = _render_parity(monkeypatch, _failed("timeout"))
    assert seen["empty"] == [("unavailable", "The environment grouping could not be read from Snowflake's "
                                             "policy-reference view.")]
    assert seen["detail"] == ["boom (timeout)"]

    _, seen = _render_parity(monkeypatch, _failed("timeout"))
    assert [k for k, *_ in seen["runs"]] == ["sec_policy_parity"]      # no sibling read under a failed grouping

    fake, seen = _render_parity(monkeypatch, _ok(pd.DataFrame()), masked=("ALFA_EDW_PRD",))
    assert seen["empty"] == [("no_data_yet", pc.PARITY_NOTHING_TO_GROUP)]
    # only production masked: nothing to group, but the unmasked environments are still named
    assert pc.SIBLINGS_LEAD in fake.text("caption")
    assert ("ALFA_EDW: no column-level masking reference in ALFA_EDW_DEV, ALFA_EDW_MGM, ALFA_EDW_PHX, ALFA_EDW_SAN, "
            "ALFA_EDW_SEA and ALFA_EDW_SIT.") in fake.text("caption")

    fake, seen = _render_parity(monkeypatch, _ok(_parity_frame(3, total=3, differing=1)))
    ((key, _sql_text, kw), (dbs_key, dbs_sql, dbs_kw)) = seen["runs"]
    assert key == "sec_policy_parity" and kw["probe"] is True and kw["tier"] == "hourly"
    assert "ACCOUNT_USAGE" not in kw["source"]
    # the sidebar's SHOW DATABASES read (same SQL, tier and max_rows: its cache entry)
    assert dbs_key == "sec_policy_parity_dbs" and dbs_sql == _sql().show_databases_sql()
    assert dbs_kw["tier"] == "metadata" and dbs_kw["max_rows"] == 0
    assert ("ALFA_EDW: no column-level masking reference in ALFA_EDW_MGM, ALFA_EDW_PHX, ALFA_EDW_SAN and "
            "ALFA_EDW_SEA.") in fake.text("caption")
    ((table, kwargs),) = seen["tables"]
    assert list(table.columns) == list(pc.PARITY_COLUMNS) and kwargs["slug"] == "masking-environment-grouping"
    caps = fake.text("caption")
    assert pc.PARITY_LEGEND in caps and "Information only, not a gap list" in caps
    assert pc.parity_summary_sentence(3, 1) in caps
    assert "Showing the first" not in caps and seen["empty"] == []

    fake, _ = _render_parity(monkeypatch, _ok(_parity_frame(3, total=1500, differing=2)))
    assert "Showing the first 3 of 1,500 names, differences first." in fake.text("caption")


def test_unmasked_sibling_read_failures_never_say_there_are_none(monkeypatch):
    """Review R1-1: a failed SHOW DATABASES read is unavailable with the error, an empty or nameless one is
    no_data_yet; neither renders the sibling lead nor a 'lists no database' line."""
    pc = _pc()
    grouping = _ok(_parity_frame(3, total=3, differing=1))
    fake, seen = _render_parity(monkeypatch, grouping, dbs=_failed("other"))
    assert seen["empty"] == [("unavailable", pc.SIBLINGS_UNCHECKED)] and seen["detail"] == ["boom (other)"]
    for dbs in (_ok(pd.DataFrame()), _ok(pd.DataFrame({"created_on": ["x"]})),
                _ok(pd.DataFrame({"NAME": [None, ""]}))):
        fake, seen = _render_parity(monkeypatch, grouping, dbs=dbs)
        assert seen["empty"] == [("no_data_yet", pc.SIBLINGS_NO_NAMES)]
        caps = fake.text("caption")
        assert pc.SIBLINGS_LEAD not in caps and pc.SIBLINGS_NONE not in caps
    # a full family: checked, and says so; a SHOW read at its row limit says the rest were not checked
    names = ["ALFA_EDW_PRD", "ALFA_EDW_DEV", "ALFA_EDW_SIT"] + [f"Z{i:03d}" for i in range(497)]
    fake, seen = _render_parity(monkeypatch, grouping, dbs=_show_dbs(names))
    assert seen["empty"] == [] and pc.SIBLINGS_NONE in fake.text("caption")
    assert len(names) == _sql().SHOW_DATABASES_LIMIT and pc.SIBLINGS_CAPPED in fake.text("caption")


def test_policy_panel_passes_its_masked_databases_to_the_grouping(monkeypatch):
    """... and (review R2-1) whether the account has masking tags, for the unmasked-sibling qualifier."""
    got: list = []
    for tags, want in ((15, True), (0, False)):
        sec, _fake, _seen = _patch(monkeypatch, {"sec_policy_cov": _ok(_probe_shape().assign(MASKING_TAGS=tags))})
        monkeypatch.setattr(sec, "_render_masking_parity",
                            lambda masked=(), *, tag_masking=None: got.append((masked, tag_masking)))
        sec._render_policy_coverage()
        assert got.pop() == (("EDW_PRD", "EDW_DEV", "EDW_SIT", "MART_A", "OPS"), want)
    assert not got


def test_unmasked_sibling_read_is_the_sidebars_cached_read():
    """No extra scan: the grouping's SHOW DATABASES read is the sidebar's exact call (run() caches by SQL, tier
    and scope, never by key), so it is a cache hit."""
    def norm(text: str) -> str:
        return " ".join(text.split()).replace("( ", "(")
    side = norm(read("app/main.py"))
    body = norm(_body(read("app/ui/pages/security.py"), "_render_unmasked_siblings"))
    for src in (side, body):
        call = src.split("run(security_sql.show_databases_sql(),", 1)[1].split(")", 1)[0]
        assert 'tier="metadata"' in call and "max_rows=0" in call and "probe" not in call, call


def _render_netpol(monkeypatch, df: pd.DataFrame):
    sec, fake, seen = _patch(monkeypatch, {"admin_netpol_ALL": _ok(df)})
    monkeypatch.setattr(sec, "guard", lambda *_a, **_k: True)
    monkeypatch.setattr(sec, "with_user_names", lambda frame, _page: frame.assign(USER=frame["USER_NAME"]))
    sec._render_admin_network_policy("ALL")
    return fake, seen


def _netpol_df(user_refs: int, account_refs: int) -> pd.DataFrame:
    return pd.DataFrame({"USER_NAME": ["A1", "A2"], "ADMIN_ROLES": ["SYSADMIN", "SYSADMIN"],
                         "USER_NETWORK_POLICY": ["NP_ADMIN", None], "NETWORK_POLICY_REFS": [5, 5],
                         "USER_POLICY_REFS": [user_refs] * 2, "ACCOUNT_POLICY_REFS": [account_refs] * 2})


def test_admin_netpol_caption_states_the_account_policy(monkeypatch):
    pc = _pc()
    fake, seen = _render_netpol(monkeypatch, _netpol_df(1, 1))
    assert seen["kpis"] and seen["empty"] == []
    assert pc.network_policy_caption(1) in fake.text("caption")
    assert "An account-level network policy is set" in fake.text("caption")

    fake, _ = _render_netpol(monkeypatch, _netpol_df(1, 0))
    assert "lists no account-level network policy" in fake.text("caption")
    assert "An account-level network policy is set" not in fake.text("caption")

    _, seen = _render_netpol(monkeypatch, _netpol_df(0, 1))
    ((state, msg),) = seen["empty"]
    assert state == "needs_setup" and "Coverage is unconfirmed, not zero" in msg
    assert seen["hint"] == [pc.ACCOUNT_POLICY_HINT] and not seen["kpis"]

    _, seen = _render_netpol(monkeypatch, _netpol_df(0, 0))
    assert seen["hint"] == [""]                                         # no account policy listed: no hint


# ------------------------------------------------------------------------------- wiring and locks ----

def test_policy_coverage_is_not_in_the_security_score():
    from app.logic.security import SECURITY_DOMAINS
    assert SECURITY_DOMAINS == ("IDENTITY", "PRIVILEGE", "CHANGE RISK", "DATA MOVEMENT", "TRUST CENTER")
    for rel in ("app/ui/security_center.py", "app/logic/security.py"):
        src = read(rel)
        for name in ("policy_coverage", "data_policy_coverage", "masking_environment_parity"):
            assert name not in src, (rel, name)
    s = _sql()
    assert "POLICY_REFERENCES" not in s.security_domain_coverage()
    assert "POLICY_REFERENCES" not in s.security_exception_queue("ALL")


def _body(src: str, name: str) -> str:
    return src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]


def test_panel_is_toggle_gated_on_exposure():
    src = read("app/ui/pages/security.py")
    cov = _body(src, "_render_policy_coverage")
    assert cov.index('key="sec_policy_cov_toggle"') < cov.index("security_sql.data_policy_coverage()")
    par = _body(src, "_render_masking_parity")
    assert par.index('key="sec_policy_parity_toggle"') < par.index("security_sql.masking_environment_parity()")
    for body in (cov, par):
        assert "probe=True" in body and 'tier="hourly"' in body
        assert "ACCOUNT_USAGE" not in body
        # information only: no Track / Action-queue write, no severity colour, no alert
        for word in ("add_to_case_button", "ACTION_QUEUE", "st.button", '"bad"', '"warn"', "delta_color"):
            assert word not in body, word
    render = _body(src, "render")
    exposure = render.split('elif section == "Exposure":', 1)[1].split("elif section", 1)[0]
    assert "_exposure_tab()" in exposure and "_render_policy_coverage()" in exposure
    assert exposure.index("_exposure_tab()") < exposure.index("_render_policy_coverage()")
    assert ("Shares are account-wide objects with no company grain; the exposure inventory and the "
            "masking/row-access policy coverage are account-wide.") in src


def test_policy_builders_have_canaries_that_fail_on_absence():
    from app.data.canary import CANARIES, EXPECTED_GAPS
    s = _sql()
    reg = dict(CANARIES)
    want = {"security.data_policy_coverage": s.data_policy_coverage(),
            "security.masking_environment_parity": s.masking_environment_parity(),
            "security.admin_network_policy_coverage": s.admin_network_policy_coverage("ALFA")}
    for name, sql in want.items():
        assert name in reg, name
        assert name not in EXPECTED_GAPS, name                   # a standard view: absence is drift, it FAILs
        assert reg[name]() == sql, name


def test_docs_describe_the_panel_and_its_canaries():
    glossary = read("FEATURE_GLOSSARY.md")
    exposure = glossary.split("### Exposure", 1)[1].split("\n### ", 1)[0]
    for part in ("masking and row-access policy coverage", "Masked columns / Tables and views / Databases / "
                 "Masking policies", "Tag-based masking line", "Row-level policies line",
                 "Environment grouping (SAME / DIFFERS)"):
        assert part in exposure, part
    pc = _pc()
    for col in (*pc.INVENTORY_COLUMNS, *pc.PARITY_COLUMNS):
        assert col in exposure, col
    row = next(ln for ln in glossary.splitlines() if ln.startswith("| **N registered statements"))
    assert "v4.603 adds 6 entries" in row and "no drift to fix" not in row
    # review R1-26: v4.604 adds five, none a declared gap
    from app.data.canary import CANARIES, EXPECTED_GAPS
    v4604 = ("security.data_policy_coverage", "security.masking_environment_parity",
             "security.admin_network_policy_coverage", "insights.warehouse_cluster_use",
             "chargeback.company_allin_showback")
    assert "v4.604 adds 5 entries" in row and "v4.604 adds 3" not in row
    reg = dict(CANARIES)
    for name in v4604:
        assert name in row and name in reg and name not in EXPECTED_GAPS, name
    # the unmasked-sibling lines are documented with the grouping
    assert "MASKED_FAMILY_DATABASES" in exposure and "SHOW DATABASES" in exposure
    assert "(#43) | Security -> Exposure |" in read("FEATURES.md")
