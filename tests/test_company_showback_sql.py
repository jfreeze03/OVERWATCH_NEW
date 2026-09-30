"""#42 Part 1 — the Company all-in showback builder (chargeback_sql.company_allin_showback).

Locks the builder to the CURRENT loaders it depends on (read from the migrations, never guessed):
the serverless arms are exactly the V139 SP_LOAD_OBJECT_COST non-query arms, the Cortex Code sources
are exactly loader arm [9]'s SOURCE literals, and every column it names exists in its mart's CREATE
TABLE. Then the SQL contract: marts only, every credit leg cut to the closed metered days, today and
the in-progress metering day never counted, company scope on the keyed legs only, COMPANY_FOR_USER on
the grouped user (V030 shape), and the output columns the pure logic reads. The generic filter matrix
(tests/test_p4_filter_matrix.py) covers parse, live filter, distinct scopes, hostile input and the
days clamp for this builder automatically.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from app.data import canary, chargeback_sql
from app.data.common import account_today_sql
from app.logic import metric_registry
from app.logic.showback import COCO_SOURCES, FRAME_COLUMNS, SERVERLESS_ARMS, STORAGE_TIERS

sqlglot = pytest.importorskip("sqlglot")
from sqlglot import exp  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_MIG = _ROOT / "snowflake" / "migrations"
_FACTS = ("FACT_METERING_DAILY", "FACT_WAREHOUSE_DAILY", "FACT_OBJECT_COST_DAILY", "FACT_AI_USAGE_DAILY",
          "FACT_STORAGE_DAILY", "FACT_STORAGE_ACCOUNT_DAILY")
_LAST_MONTH = (date(2026, 8, 1), date(2026, 9, 1))


def _mig_num(p: Path) -> int:
    return int(re.match(r"V(\d+)", p.name).group(1))  # type: ignore[union-attr]


def _latest_proc_body(proc: str) -> tuple[int, str]:
    pat = re.compile(r"CREATE\s+OR\s+REPLACE\s+PROCEDURE\s+DBA_MAINT_DB\.OVERWATCH\." + proc + r"\s*\(",
                     re.IGNORECASE)
    best: tuple[int, str] | None = None
    for p in _MIG.glob("V*.sql"):
        text = p.read_text(encoding="utf-8")
        for m in pat.finditer(text):
            rest = text[m.end():]
            a = rest.index("$$")
            body = rest[a + 2: rest.index("$$", a + 2)]
            if best is None or _mig_num(p) >= best[0]:
                best = (_mig_num(p), body)
    assert best is not None, f"no CREATE PROCEDURE for {proc}"
    return best


def _table_columns(table: str) -> set[str]:
    """Columns of a mart from its CREATE TABLE plus any later ADD COLUMN, across the migrations."""
    cols: set[str] = set()
    create = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TRANSIENT\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                        r"DBA_MAINT_DB\.OVERWATCH\." + table + r"\s*\((.*?)\n\);", re.S | re.I)
    add = re.compile(r"ALTER\s+TABLE\s+DBA_MAINT_DB\.OVERWATCH\." + table
                     + r"\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I)
    for p in sorted(_MIG.glob("V*.sql"), key=_mig_num):
        text = p.read_text(encoding="utf-8")
        for m in create.finditer(text):
            for line in m.group(1).splitlines():
                c = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s+[A-Z]", line)
                if c:
                    cols.add(c.group(1))
        cols |= {c.upper() for c in add.findall(text)}
    assert cols, f"no CREATE TABLE found for {table}"
    return cols


def _referenced_columns(sql: str) -> dict[str, set[str]]:
    """{mart: columns the SQL reads from it}, resolved per SELECT scope (alias -> real table)."""
    tree = sqlglot.parse_one(sql, read="snowflake")
    found: dict[str, set[str]] = {}
    for sel in tree.find_all(exp.Select):
        aliases = {t.alias_or_name.upper(): t.name.upper()
                   for t in sel.find_all(exp.Table)
                   if t.find_ancestor(exp.Select) is sel and t.name.upper() in _FACTS}
        for col in sel.find_all(exp.Column):
            if col.find_ancestor(exp.Select) is not sel:
                continue
            tbl = aliases.get(col.table.upper())
            if tbl:
                found.setdefault(tbl, set()).add(col.name.upper())
    return found


def _cte(sql: str, name: str) -> str:
    """One CTE body from the builder text (up to the next CTE opener)."""
    after = sql.split(f"{name} AS (", 1)[1]
    return re.split(r"\n\), \w+ AS \(|\n\)\nSELECT ", after, maxsplit=1)[0]


def test_serverless_arms_match_the_current_object_cost_loader():
    version, body = _latest_proc_body("SP_LOAD_OBJECT_COST")
    assert version >= 139
    non_query = set(re.findall(r"'(?:TABLE|MATERIALIZED_VIEW|TASK|PIPE)',\s*'([A-Z_]+)'", body))
    assert non_query == set(SERVERLESS_ARMS)
    # every other arm the loader writes is a query-compute slice (or its residual)
    query_arms = set(re.findall(r"'(QUERY_COMPUTE[A-Z_]*)'", body))
    assert query_arms == {"QUERY_COMPUTE_READ", "QUERY_COMPUTE_WRITE", "QUERY_COMPUTE_RESIDUAL"}
    # one INSERT per non-query arm + the query split + the residual: a new arm fails here first
    inserts = body.count("INSERT INTO DBA_MAINT_DB.OVERWATCH.FACT_OBJECT_COST_DAILY")
    assert inserts == len(SERVERLESS_ARMS) + 2, inserts


def test_coco_sources_match_the_loader_code_arms():
    version, body = _latest_proc_body("SP_LOAD_MARTS_V27")
    assert version >= 159
    assert re.findall(r"TOKEN_CREDITS, TOKENS, '(\w+)'", body) == list(COCO_SOURCES)
    assert "Desktop" not in COCO_SOURCES
    # the copy says Cortex Code Desktop is not read: true while no migration or builder names its view
    for p in list(_MIG.glob("V*.sql")) + list((_ROOT / "app").rglob("*.py")):
        assert "CORTEX_CODE_DESKTOP" not in p.read_text(encoding="utf-8").upper(), p.name


def test_builder_columns_exist_in_the_mart_definitions():
    refs = _referenced_columns(chargeback_sql.company_allin_showback(30, "ALFA"))
    assert set(refs) == set(_FACTS), sorted(refs)
    for table, cols in refs.items():
        defined = _table_columns(table)
        assert cols <= defined, (table, sorted(cols - defined))
    acct = _table_columns("FACT_STORAGE_ACCOUNT_DAILY")
    for tier in STORAGE_TIERS:
        assert f"{tier}_BYTES" in acct, tier
    assert refs["FACT_STORAGE_ACCOUNT_DAILY"] >= {f"{t}_BYTES" for t in STORAGE_TIERS}


def test_builder_excludes_every_query_compute_arm():
    for company in ("ALL", "ALFA"):
        sql = chargeback_sql.company_allin_showback(30, company)
        assert "QUERY_COMPUTE" not in sql
        assert ("o.COST_ARM IN ('CLUSTERING', 'MV_REFRESH', 'SEARCH_OPT', 'SERVERLESS_TASK', 'SNOWPIPE')"
                in sql)


@pytest.mark.parametrize("company", ["ALL", "ALFA", "Trexis", "UNKNOWN"])
@pytest.mark.parametrize("bounds", [None, _LAST_MONTH])
def test_builder_reads_marts_only_and_parses(company, bounds):
    sql = chargeback_sql.company_allin_showback(30, company, bounds=bounds)
    sqlglot.parse(sql, dialect="snowflake")
    assert "ACCOUNT_USAGE" not in sql.upper()
    for fact in _FACTS:
        assert f"DBA_MAINT_DB.OVERWATCH.{fact}" in sql, fact
    real = {t.name.upper() for t in sqlglot.parse_one(sql, read="snowflake").find_all(exp.Table)
            if t.catalog}
    assert real == set(_FACTS)


def test_every_credit_leg_is_cut_to_metered_days():
    sql = chargeback_sql.company_allin_showback(30, "ALL")
    for alias in ("m", "w", "o", "a"):
        assert f"JOIN mdays d ON d.DAY = {alias}.DAY" in sql, alias
    assert "JOIN sdays d ON d.DAY = sd.DAY" in sql and "JOIN sdays d ON d.DAY = t.DAY" in sql
    # the storage days are the account-storage days AMONG the metered days, so a day daily
    # metering skipped is left out of every row (the interior-gap note says exactly that)
    assert "FACT_STORAGE_ACCOUNT_DAILY t JOIN mdays d ON d.DAY = t.DAY" in _cte(sql, "sdays")


def test_span_drops_the_in_progress_metering_day_and_today():
    today = account_today_sql()
    trailing = chargeback_sql.company_allin_showback(30, "ALL")
    assert "LEAST(w.W_HI, c.M_LAST) AS HI" in trailing
    assert "WHERE m.DAY >= s.LO AND m.DAY < s.HI" in trailing              # HI itself never counts
    assert f"SELECT DATEADD('day', -30, {today}) AS W_LO, {today} AS W_HI" in trailing
    bounded = chargeback_sql.company_allin_showback(30, "ALL", bounds=_LAST_MONTH)
    assert f"SELECT '2026-08-01'::DATE AS W_LO, LEAST('2026-09-01'::DATE, {today}) AS W_HI" in bounded
    for sql in (trailing, bounded):
        assert "CURRENT_DATE()" not in sql
        assert "GREATEST(w.W_LO, c.M_FIRST) AS LO" in sql


def test_company_scope_filters_keyed_legs_only():
    alfa = chargeback_sql.company_allin_showback(30, "ALFA")
    assert "w.COMPANY = 'ALFA'" in _cte(alfa, "wh")
    assert "COALESCE(o.COMPANY, 'UNKNOWN') = 'ALFA'" in _cte(alfa, "sl")
    assert "c.COMPANY = 'ALFA'" in _cte(alfa, "coco")
    assert "sd.COMPANY = 'ALFA'" in _cte(alfa, "stor_db")
    for unscoped in ("mcov", "mdays", "sdays", "metering", "coco_user", "stor_acct"):
        assert "'ALFA'" not in _cte(alfa, unscoped), unscoped
    tail = alfa.split("FROM win w\n", 1)[1]              # the output arms + coverage rows
    assert "'ALFA'" not in tail
    everything = chargeback_sql.company_allin_showback(30, "ALL")
    assert "COMPANY = '" not in everything


def test_coco_company_is_resolved_per_grouped_user():
    sql = chargeback_sql.company_allin_showback(30, "ALL")
    assert sql.count("COMPANY_FOR_USER(") == 1
    coco = _cte(sql, "coco")
    assert "SELECT u.CREDITS, DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(u.USER_NAME) AS COMPANY" in coco
    assert "FROM coco_user u" in coco and "GROUP BY" not in coco
    assert not re.search(r"SUM\([^)]*COMPANY_FOR_USER", sql)
    assert "GROUP BY a.USER_NAME" in _cte(sql, "coco_user")
    # no user name leaves the query: the output carries company totals only
    tail = sql.split("FROM win w\n", 1)[1]
    assert "USER_NAME" not in tail


def test_output_columns_are_the_logic_contract():
    for company in ("ALL", "Trexis"):
        tree = sqlglot.parse_one(chargeback_sql.company_allin_showback(7, company), read="snowflake")
        assert tree.named_selects == list(FRAME_COLUMNS)


def test_canary_registered_not_an_expected_gap():
    entries = dict(canary.CANARIES)
    assert "chargeback.company_allin_showback" in entries
    assert entries["chargeback.company_allin_showback"]() == chargeback_sql.company_allin_showback(2, "ALFA")
    # every fact it reads is a core OVERWATCH table: an absence is a real failure, never a declared gap
    assert "chargeback.company_allin_showback" not in canary.EXPECTED_GAPS


def test_metric_registry_contract():
    m = metric_registry.get("company_allin_showback")
    assert m is not None and m.method == metric_registry.BILLED
    assert m.window == "trailing-complete-days" and m.partial_day == "excluded"
    assert m.filters == ("company",) and m.unit == "USD"
    sql = chargeback_sql.company_allin_showback(30, "ALL")
    assert set(m.required_sources) == set(_FACTS)
    for src in m.required_sources:
        assert src in sql, src
    assert metric_registry.validate() == []
