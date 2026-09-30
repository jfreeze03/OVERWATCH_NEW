"""Executed harness for the Next-Fifty #43 builders (security_sql.data_policy_coverage and
masking_environment_parity): the REAL builder SQL runs in sqlite over seeded POLICY_REFERENCES rows (the view's FQN
substituted; IFF / COUNT_IF / LISTAGG / REGEXP_LIKE / REGEXP_REPLACE / REGEXP_SUBSTR shimmed with Snowflake's
semantics), then through app.logic.policy_coverage.

Locks the semantics the string locks in tests/test_policy_coverage.py cannot (review r1):
- R1-19: every distinct count is keyed on a fully qualified name, so the same schema.table.column in two databases
  (or the same table.column in two schemas) counts twice, never S1a's unqualified once;
- R1-20: tag-domain rows never leak into the column-level totals, the database count or the inventory;
- R1-24: the not-ACTIVE count is computed in SQL (masking rows only, status upper-cased), not a constant;
- R1-2: MASKING_TAGS counts distinct tags: one tag carrying two policies counts once;
- R1-1: the grouping's MASKED_FAMILY_DATABASES counts only a family's databases with column masking.
"""

from __future__ import annotations

import re
import sqlite3

import pandas as pd
import pytest

from app.data import security_sql
from app.logic import policy_coverage as pc

_PR = "SNOWFLAKE.ACCOUNT_USAGE.POLICY_REFERENCES"
_COLS = ("POLICY_DB", "POLICY_SCHEMA", "POLICY_NAME", "POLICY_KIND", "REF_DATABASE_NAME", "REF_SCHEMA_NAME",
         "REF_ENTITY_NAME", "REF_ENTITY_DOMAIN", "REF_COLUMN_NAME", "POLICY_STATUS")


def _to_sqlite(sql: str) -> str:
    assert sql.count(_PR) == 1
    sql = sql.replace(_PR, "POLICY_REFERENCES")
    # LISTAGG([DISTINCT] x, sep) [WITHIN GROUP (ORDER BY c)] -> a shim that sorts (the builders order by the
    # value itself, or by the leading database name of "DB (n)")
    sql = re.sub(r"\s*WITHIN GROUP \(ORDER BY \w+\)", "", sql)
    sql = sql.replace("LISTAGG(DISTINCT ", "LISTAGG_DISTINCT(")
    assert "WITHIN GROUP" not in sql and "SNOWFLAKE." not in sql
    return sql


def _iff(cond, a, b):
    return a if cond else b


def _regexp_like(v, p):                 # Snowflake anchors the whole subject
    return None if v is None else int(re.fullmatch(p, v) is not None)


def _regexp_replace(v, p, r):
    return None if v is None else re.sub(p, r, v)


def _regexp_substr(v, p):
    if v is None:
        return None
    m = re.search(p, v)
    return m.group(0) if m else None


class _CountIf:
    def __init__(self):
        self.n = 0

    def step(self, c):
        if c:
            self.n += 1

    def finalize(self):
        return self.n


class _ListAgg:
    """Snowflake LISTAGG: NULLs skipped, '' when every input is NULL."""

    distinct = False

    def __init__(self):
        self.vals: list[str] = []
        self.sep = ","

    def step(self, v, sep):
        self.sep = sep
        if v is not None:
            self.vals.append(str(v))

    def finalize(self):
        vals = sorted(set(self.vals)) if self.distinct else sorted(self.vals)
        return self.sep.join(vals)


class _ListAggDistinct(_ListAgg):
    distinct = True


@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.create_function("IFF", 3, _iff)
    c.create_function("REGEXP_LIKE", 2, _regexp_like)
    c.create_function("REGEXP_REPLACE", 3, _regexp_replace)
    c.create_function("REGEXP_SUBSTR", 2, _regexp_substr)
    c.create_aggregate("COUNT_IF", 1, _CountIf)
    c.create_aggregate("LISTAGG", 2, _ListAgg)
    c.create_aggregate("LISTAGG_DISTINCT", 2, _ListAggDistinct)
    c.execute(f"CREATE TABLE POLICY_REFERENCES ({', '.join(_COLS)})")
    yield c
    c.close()


def _seed(c, rows) -> None:
    c.executemany(f"INSERT INTO POLICY_REFERENCES VALUES ({', '.join('?' * len(_COLS))})", rows)


def _query(c, sql: str) -> pd.DataFrame:
    cur = c.execute(_to_sqlite(sql))
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def _mask(db_, sch, obj, col, *, policy=("GOV", "POL", "MP_PII"), status="ACTIVE", domain="TABLE"):
    return (*policy, "MASKING_POLICY", db_, sch, obj, domain, col, status)


# The same CORE.CUST.SSN in two databases, the same CUST.SSN in two schemas of one database, one policy name in
# two policy schemas; tags (one carrying two policies) in a tag-only database AND in a masked database; one masking
# reference not ACTIVE plus a lower-case 'active' one; a non-ACTIVE row-access row; a spelled-out kind.
_ROWS = [
    _mask("EDW_PRD", "CORE", "CUST", "SSN"),
    _mask("EDW_DEV", "CORE", "CUST", "SSN"),
    _mask("EDW_PRD", "STAGE", "CUST", "SSN", policy=("GOV", "POL2", "MP_PII"), status="active"),
    _mask("EDW_DEV", "CORE", "ACCT", "TIN", status="MULTIPLE_MASKING_POLICY_ASSIGNED_TO_THE_COLUMN"),
    _mask("GOV", "TAGS", "PII", None, policy=("GOV", "POL", "MP_TAG_STR"), domain="TAG"),
    _mask("GOV", "TAGS", "PII", None, policy=("GOV", "POL", "MP_TAG_NUM"), domain="TAG"),
    _mask("EDW_PRD", "TAGS", "PII2", None, policy=("GOV", "POL", "MP_TAG_STR"), domain="TAG"),
    ("GOV", "POL", "RAP", "row access policy", "EDW_PRD", "CORE", "CUST", "TABLE", None, "INACTIVE"),
    ("GOV", "POL", "JP", "JOIN POLICY", "EDW_PRD", "CORE", "CUST", "TABLE", None, "ACTIVE"),
    (None, None, "NP", "NETWORK_POLICY", None, None, "ACCT", "ACCOUNT", None, "ACTIVE"),
]


def test_coverage_counts_on_fully_qualified_names(db):
    """R1-19: an unqualified COLUMN_FQN / ENTITY_FQN / POLICY_FQN (S1a's key) would collapse these to 2 / 2 / 3."""
    _seed(db, _ROWS)
    out = _query(db, security_sql.data_policy_coverage())
    cov = pc.summarize_policy_coverage(out)
    assert cov is not None
    assert cov.masked_columns == 4          # EDW_PRD.CORE.CUST.SSN, EDW_DEV.CORE.CUST.SSN, EDW_PRD.STAGE.CUST.SSN,
    assert cov.masked_objects == 4          # EDW_DEV.CORE.ACCT.TIN -- and their four tables
    assert cov.masking_policies == 4        # GOV.POL.MP_PII, GOV.POL2.MP_PII, GOV.POL.MP_TAG_STR, GOV.POL.MP_TAG_NUM
    inv = pc.database_inventory(out).set_index("DATABASE_NAME")
    assert inv.loc["EDW_PRD", "MASKED_COLUMNS"] == 2 and inv.loc["EDW_PRD", "MASKED_OBJECTS"] == 2
    assert inv.loc["EDW_PRD", "MASKING_POLICIES"] == 2     # the two policy schemas, not the tag's policy


def test_tag_rows_never_reach_the_column_level_counts(db):
    """R1-20: without the REF_DOMAIN <> 'TAG' filters, the objects would read 6, the databases 3 (GOV) and the
    inventory would gain a GOV row; EDW_PRD's tag would add an object and a policy to its row."""
    _seed(db, _ROWS)
    out = _query(db, security_sql.data_policy_coverage())
    cov = pc.summarize_policy_coverage(out)
    assert cov is not None
    assert (cov.masked_objects, cov.masked_databases) == (4, 2)
    assert list(pc.database_inventory(out)["DATABASE_NAME"]) == ["EDW_DEV", "EDW_PRD"]
    # the tag line: distinct tags (review R1-2: GOV.TAGS.PII carries two policies and counts once)
    assert (cov.masking_tags, cov.masking_tag_databases, cov.tag_masking_policies) == (2, 2, 2)
    assert pc.tag_masking_sentence(cov).startswith("Tag-based masking: 2 tags in 2 databases carry a masking policy")


def test_not_active_count_is_computed_in_sql(db):
    """R1-24: a constant 0, a dropped UPPER or a count over every kind would each change these numbers."""
    _seed(db, _ROWS)
    out = _query(db, security_sql.data_policy_coverage())
    cov = pc.summarize_policy_coverage(out)
    assert cov is not None
    assert cov.not_active_refs == 1 and cov.not_active_statuses == "MULTIPLE_MASKING_POLICY_ASSIGNED_TO_THE_COLUMN"
    assert pc.not_active_sentence(cov) == ("1 masking reference reports a status other than ACTIVE: "
                                           "MULTIPLE_MASKING_POLICY_ASSIGNED_TO_THE_COLUMN.")
    inv = pc.database_inventory(out).set_index("DATABASE_NAME")
    assert (inv.loc["EDW_DEV", "REFS_NOT_ACTIVE"], inv.loc["EDW_PRD", "REFS_NOT_ACTIVE"]) == (1, 0)
    # kinds are normalised: the spelled-out row-access kind is counted, the join policy is an "other" kind
    assert (cov.row_access_objects, cov.row_access_policies) == (1, 1)
    assert cov.other_policy_kinds == "JOIN_POLICY"
    assert "ENVIRONMENT" in out.columns and set(inv["ENVIRONMENT"]) == {"PRD", "DEV"}


def test_no_masking_still_returns_the_totals_row(db):
    _seed(db, [_ROWS[-1]])                                   # one account-level network policy only
    out = _query(db, security_sql.data_policy_coverage())
    assert len(out) == 1 and out.loc[0, "DATABASE_NAME"] is None
    cov = pc.summarize_policy_coverage(out)
    assert cov is not None and cov.masked_columns == 0 and cov.not_active_refs == 0 and cov.other_policy_kinds == ""
    assert pc.database_inventory(out).empty


def test_parity_counts_only_masked_family_databases(db):
    """R1-1: EDW_SAN exists with tag masking only, so it is no family database here: MASKED_FAMILY_DATABASES is 3
    and EDW_SAN is in no NO_MASKING_REF_IN (the page lists it from SHOW DATABASES instead). MART_A is alone in its
    family, so nothing of it is grouped."""
    _seed(db, [
        _mask("EDW_PRD", "CORE", "CUST", "SSN"), _mask("EDW_PRD", "CORE", "CUST", "DOB"),
        _mask("EDW_DEV", "CORE", "CUST", "SSN"), _mask("EDW_DEV", "CORE", "CUST", "DOB"),
        _mask("EDW_SIT", "CORE", "CUST", "SSN"),
        _mask("EDW_PRD", "CORE", "ACCT", "TIN"), _mask("EDW_DEV", "CORE", "ACCT", "TIN"),
        _mask("EDW_DEV", "CORE", "ACCT", "ACCTNO"),
        *(_mask(d, "CORE", "ADDR", "ZIP") for d in ("EDW_PRD", "EDW_DEV", "EDW_SIT")),
        _mask("EDW_SAN", "TAGS", "PII", None, domain="TAG"),
        _mask("MART_A", "CORE", "CUST", "SSN"),
    ])
    out = _query(db, security_sql.masking_environment_parity())
    view = pc.parity_view(out)
    assert list(view.columns) == list(pc.PARITY_COLUMNS)
    assert list(view["OBJECT_NAME"]) == ["ACCT", "CUST", "ADDR"]            # differences first
    assert set(view["MASKED_FAMILY_DATABASES"]) == {3} and set(view["DATABASE_FAMILY"]) == {"EDW"}
    rows = view.set_index("OBJECT_NAME")
    assert rows.loc["ACCT", "PARITY"] == "DIFFERS" and rows.loc["ACCT", "DATABASES_MASKED"] == 2
    assert rows.loc["ACCT", "MASKED_IN"] == "EDW_DEV (2), EDW_PRD (1)" and rows.loc["ACCT", "NO_MASKING_REF_IN"] == "EDW_SIT"
    assert rows.loc["CUST", "PARITY"] == "DIFFERS" and rows.loc["CUST", "COLUMN_SETS"] == 2
    assert rows.loc["CUST", "NO_MASKING_REF_IN"] is None
    assert rows.loc["ADDR", "PARITY"] == "SAME" and rows.loc["ADDR", "DATABASES_MASKED"] == 3
    assert pc.parity_counts(out) == (3, 2)
    assert "EDW_SAN" not in " ".join(str(v) for v in view.to_numpy().ravel())
    # the page's sibling list names it, from the SHOW DATABASES names
    masked = ["EDW_PRD", "EDW_DEV", "EDW_SIT", "MART_A"]
    assert pc.unmasked_family_databases(masked, [*masked, "EDW_SAN", "MART_B"]) == (
        pc.FamilySiblings("EDW", ("EDW_SAN",)), pc.FamilySiblings("MART", ("MART_B",)))
