"""v4.612.0 (owner ask 2026-10-08): the Cortex Code models builder, cortex_sql.coco_model_usage_daily.

"my boss addressed coco usage. We need to track and drill down by user which models they select when using coco."
ONE live read of the unified SNOWFLAKE_COCO_USAGE_HISTORY (Snowsight, CLI and Desktop) behind Cost > Chargeback &
AI > Cortex Code models; every lens is a pandas fold (tests/test_coco_models_logic.py). These locks pin the SQL
contract the folds read and the shape rules it was written to (never run against Snowflake by the agent: the
owner's runbox probe PROBES_COCO_MODELS_2026-10-08.sql block C18 runs this exact CTE chain).
"""

from __future__ import annotations

import inspect
import re

import pytest

from app.companies import COMPANIES
from app.data import canary, cortex_sql
from app.data.cortex_sql import LIVE_DERIVE_DAYS
from app.logic import cortex as cortex_logic

sqlglot = pytest.importorskip("sqlglot")
from sqlglot import exp  # noqa: E402

_AU_RE = re.compile(r"SNOWFLAKE\.ACCOUNT_USAGE\.(\w+)")

#: The fold contract (app.logic.cortex.coco_*): exactly these columns, in this order.
_COLUMNS = [
    "USAGE_DATE", "USER_NAME", "SOURCE", "ROLE_NAME", "MODEL_NAME",
    "REQUESTS_USING", "MAIN_REQUESTS", "REQUEST_TOKEN_CREDITS", "REQUEST_TOKENS",
    "COCO_CREDITS", "COCO_CREDITS_INPUT", "COCO_CREDITS_CACHE_READ", "COCO_CREDITS_CACHE_WRITE",
    "COCO_CREDITS_OUTPUT", "COCO_CREDITS_OTHER",
    "TOKENS_INPUT", "TOKENS_CACHE_READ", "TOKENS_CACHE_WRITE", "TOKENS_OUTPUT", "TOKENS_OTHER",
    "FIRST_TS", "LAST_TS",
]


def _sql(company: str = "ALL") -> str:
    return cortex_sql.coco_model_usage_daily(company)


def _cte(sql: str, name: str) -> str:
    """The body of one CTE (up to the next top-level CTE or the final SELECT)."""
    body = sql.split(f"{name} AS (", 1)[1]
    return re.split(r"\n\),\n\w+ AS \(|\n\)\nSELECT", body, maxsplit=1)[0]


@pytest.mark.parametrize("company", COMPANIES)
def test_parses_for_every_company_with_exactly_the_fold_columns(company):
    tree = sqlglot.parse_one(_sql(company), read="snowflake")
    assert list(tree.named_selects) == _COLUMNS
    # the folds read every column by name: the module's own contract tuple is the same list
    assert list(cortex_logic._COCO_COLUMNS) == _COLUMNS


def test_reads_only_the_unified_view_and_users():
    sql = _sql("ALFA")
    assert set(_AU_RE.findall(sql)) == {"SNOWFLAKE_COCO_USAGE_HISTORY", "USERS"}
    for banned in ("CORTEX_CODE_DESKTOP", "CORTEX_CODE_SNOWSIGHT", "CORTEX_CODE_CLI"):
        assert banned not in sql.upper(), banned


def test_days_independent_bounded_and_capped():
    assert list(inspect.signature(cortex_sql.coco_model_usage_daily).parameters) == ["company"]
    assert inspect.signature(cortex_sql.coco_model_usage_daily).parameters["company"].default == "ALL"
    sql = _sql()
    assert f"DATEADD('day', -{LIVE_DERIVE_DAYS}, CURRENT_TIMESTAMP())" in sql
    assert sql.rstrip().endswith("LIMIT 200000")
    # one cache entry per company: the text never depends on anything but the company
    assert _sql("ALFA") == _sql("ALFA") and _sql("ALL") != _sql("ALFA")


def test_day_key_is_the_central_day():
    sql = _sql()
    assert "CONVERT_TIMEZONE('America/Chicago', C.USAGE_TIME)::DATE AS USAGE_DATE" in sql
    assert "USAGE_TIME::DATE" not in sql.replace("C.USAGE_TIME)::DATE", "")


def test_every_leaf_is_coalesced_and_read_as_a_double():
    sql = _sql()
    leaves = re.findall(r"TRY_TO_DOUBLE\(TO_VARCHAR\([FT]\.VALUE[^)]*\)\)", sql)
    assert len(leaves) == 10                                   # 5 credit + 5 token leaves
    for leaf in leaves:
        assert f"COALESCE({leaf}, 0)" in sql, leaf
    for name in ("input", "cache_read_input", "cache_write_input", "output"):
        assert f"F.VALUE:{name}" in sql and f"T.VALUE:{name}" in sql, name
    # TRY_TO_NUMBER is scale 0: 0.0123 credits would read 0
    assert "TRY_TO_NUMBER" not in sql


def test_each_flatten_sits_alone_in_its_own_cte_and_no_lateral_is_left_of_a_join():
    sql = _sql()
    assert "LATERAL FLATTEN(INPUT => B.CREDITS_GRANULAR, OUTER => TRUE) F" in sql   # no-breakdown requests kept
    assert "LATERAL FLATTEN(INPUT => B.TOKENS_GRANULAR) T" in sql
    # review r1: a model's tokens come only from requests that also carry a credit breakdown
    assert "WHERE ARRAY_SIZE(OBJECT_KEYS(B.CREDITS_GRANULAR)) > 0" in _cte(sql, "tk_agg")
    assert sql.count("LATERAL FLATTEN(") == 2
    assert _cte(sql, "cr").count("LATERAL") == 1 and _cte(sql, "tk_agg").count("LATERAL") == 1
    # 001072: the only LEFT JOIN is the users join on the merged CTE, never on a LATERAL
    assert sql.count("LEFT JOIN") == 1
    assert "FROM merged m\n    LEFT JOIN users1 u ON u.USER_ID = m.USER_KEY" in sql
    # tokens and credits meet on the five equality keys, FULL OUTER (a one-sided model is kept)
    assert "FROM cr_agg c\n    FULL OUTER JOIN tk_agg t" in sql
    # the main model is ranked per request (FLATTEN SEQ), not per REQUEST_ID
    assert "PARTITION BY cr.ROW_SEQ" in sql and "F.SEQ AS ROW_SEQ" in sql


def test_interface_and_role_mapping():
    sql = _sql()
    for raw, label in (("snowsight", "Snowsight"), ("cli", "CLI"), ("desktop", "Desktop")):
        assert f"WHEN '{raw}' THEN '{label}'" in sql, raw
    assert "ELSE COALESCE(NULLIF(TRIM(C.INTERFACE), ''), '(unknown)')" in sql      # never NULL
    assert ("COALESCE(NULLIF(TRIM(C.METADATA:role_name::STRING), ''), '(not recorded)') AS ROLE_NAME") in sql
    # users key on USERS.NAME by USER_ID (deduplicated), the cortex_code_user_daily / COMPANY_FOR_USER key
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY U.USER_ID ORDER BY U.CREATED_ON DESC NULLS LAST) = 1" in sql
    assert "C.USER_NAME" not in sql                            # the view's own login column is not read


def test_company_scope_runs_once_per_distinct_user_after_aggregation():
    assert "COMPANY_FOR_USER" not in _sql("ALL")
    assert "WHERE 1 = 1" in _sql("ALL")
    for company in COMPANIES:
        if company == "ALL":
            continue
        sql = _sql(company)
        assert sql.count("COMPANY_FOR_USER") == 1, company
        where = sql.split("FROM named n\nWHERE ", 1)[1]
        assert where.startswith("n.USER_NAME IN (SELECT s.USER_NAME FROM (SELECT DISTINCT d.USER_NAME FROM named d) "
                                "s WHERE "), company
        assert "COMPANY_FOR_USER(s.USER_NAME)" in where, company
        # V030: the UDF is on a plain column, never inside an aggregate
        tree = sqlglot.parse_one(sql, read="snowflake")
        for agg in tree.find_all(exp.AggFunc):
            assert "COMPANY_FOR_USER" not in agg.sql().upper(), company


def test_no_division_and_no_rates_in_sql():
    sql = _sql("ALFA")
    tree = sqlglot.parse_one(sql, read="snowflake")
    assert not list(tree.find_all(exp.Div))                     # every ratio is a guarded pandas fold
    assert "/" not in sql.replace("'America/Chicago'", "")
    assert "2.2" not in sql and "3.68" not in sql and "PRICE" not in sql.upper()


def test_the_no_breakdown_literal_is_the_logic_constant():
    assert f"'{cortex_logic.COCO_NO_BREAKDOWN}'" in _sql()
    assert _sql().count(f"'{cortex_logic.COCO_NO_BREAKDOWN}'") == 2      # the credit and the token side


def test_canary_is_the_exact_app_text_and_a_declared_gap():
    reg = dict(canary.CANARIES)
    assert reg["cortex.coco_model_usage_daily"]() == cortex_sql.coco_model_usage_daily("ALFA")
    assert "cortex.coco_model_usage_daily" in canary.EXPECTED_GAPS
    assert "cortex.coco_model_usage_daily" not in canary.MIGRATION_GATED          # app-only: no migration


def test_every_column_read_is_attributed_to_its_table():
    from tests.test_canary_coverage import _read_columns
    cols, unattributed = _read_columns(_sql("ALFA"))
    assert not unattributed, unattributed
    by_table: dict[str, set[str]] = {}
    for table, col in cols:
        by_table.setdefault(table.split(".")[-1], set()).add(col)
    assert by_table == {
        "SNOWFLAKE_COCO_USAGE_HISTORY": {"USER_ID", "USAGE_TIME", "INTERFACE", "METADATA", "TOKEN_CREDITS",
                                         "TOKENS", "CREDITS_GRANULAR", "TOKENS_GRANULAR"},
        "USERS": {"USER_ID", "NAME", "CREATED_ON"},
    }, by_table


def test_metric_contract_and_cost_coverage_name_the_model_read():
    from app.logic import cost_coverage, metric_registry
    assert metric_registry.validate() == []
    metric = metric_registry.get("coco_user_model_spend")
    assert metric is not None and metric.required_sources == ("ACCOUNT_USAGE.SNOWFLAKE_COCO_USAGE_HISTORY",)
    assert metric.filters == ("company",) and metric.unit == "USD"
    for col in ("MAIN_REQUESTS", "REQUESTS_USING", "MOST_USED_MODEL", "TOP_MODEL", "MODEL_SHARE_PCT"):
        assert metric_registry.COLUMN_HELP[col].count("$") <= 1, col
    assert "cortex_sql.coco_model_usage_daily" in cost_coverage.drill_builders_for("SNOWFLAKE_COCO_SNOWSIGHT")
    assert cost_coverage._coverage_for("SNOWFLAKE_COCO_SNOWSIGHT")[0] == "User / model / day"
