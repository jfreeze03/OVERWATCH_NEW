"""Next-Fifty #27 — mart27_sql.alloc_xdim_day_drivers, the below-warehouse day-drill reader.

String/shape locks (parse, facts only, window literals, exact warehouse, company scope + masking,
validation, clamps, injection, canary, the row bound) plus an EXECUTED sqlite harness: the real SQL
runs over seeded FACT_COST_ALLOC_XDIM_DAILY / FACT_WAREHOUSE_DAILY rows and through
anomaly_explain.explain_below_warehouse, proving the spine, the top-N bucket and the other-company
mask keep every table adding up to the warehouse's metered move.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.config import DEFAULT_MAX_ROWS
from app.data import mart27_sql
from app.logic.anomaly_explain import UNALLOCATED_LABEL, explain_below_warehouse

sqlglot = pytest.importorskip("sqlglot")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_p4_filter_matrix import _PAYLOADS, _escapes_a_literal  # noqa: E402 — shared detector

_D = date(2026, 9, 20)
_B = mart27_sql.alloc_xdim_day_drivers


def _sql(company: str = "ALL", **kw) -> str:
    return _B("WH_X", _D.isoformat(), company, **kw)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("company", ["ALL", "ALFA", "Trexis", "UNKNOWN"])
def test_parses_with_the_long_shape(company):
    tree = sqlglot.parse_one(_sql(company), dialect="snowflake")
    assert tree.named_selects == ["DAY", "DIMENSION", "KEY_NAME", "CREDITS"]


def test_reads_the_two_facts_only_on_their_own_day_column():
    sql = _sql("ALFA")
    assert "FACT_COST_ALLOC_XDIM_DAILY" in sql and "FACT_WAREHOUSE_DAILY" in sql
    for banned in ("ACCOUNT_USAGE", "START_TIME", "CURRENT_DATE", "CURRENT_TIMESTAMP", "ILIKE", "LIMIT"):
        assert banned not in sql, banned


def test_window_literals_exact_warehouse_and_metered_basis():
    sql = _sql()
    assert "x.DAY >= '2026-09-06'::DATE AND x.DAY <= '2026-09-20'::DATE" in sql   # [D-14, D]
    assert "x.WAREHOUSE_NAME = 'WH_X'" in sql and "f.WAREHOUSE_NAME = 'WH_X'" in sql
    assert "ROUND(SUM(f.CREDITS_TOTAL), 6)" in sql and "CREDITS_COMPUTE" not in sql
    assert "f.DAY IN (SELECT DAY FROM spine)" in sql
    assert "SELECT DAY, 'SPINE', '', 0 FROM spine" in sql
    assert "GROUP BY f.DAY, f.WAREHOUSE_NAME" in sql                   # by column, not the literal's position
    # the spine is the fact's loaded days — NOT scoped to the warehouse (a quiet day is still a day)
    spine = sql.split("spine AS (", 1)[1].split(")", 1)[0]
    assert "WAREHOUSE_NAME" not in spine and "COMPANY" not in spine
    # the delta-proxy rank keeps the movers in the top N
    assert "PARTITION BY DIMENSION ORDER BY" in sql and "/ 14) DESC, KEY_NAME" in sql
    assert "IFF(r.RN <= 40, m.KEY_NAME," in sql


def test_company_scope_and_masking():
    sql_all = _sql("ALL")
    for token in ("COMPANY_FOR_WAREHOUSE", "COMPANY_FOR_USER", "other-company", "f.COMPANY"):
        assert token not in sql_all, token
    alfa = _sql("ALFA")
    assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(x.WAREHOUSE_NAME) = 'ALFA'" in alfa
    assert "f.COMPANY = 'ALFA'" in alfa
    assert "THEN '(other-company users)'" in alfa and "THEN '(other-company databases)'" in alfa
    # masked, never dropped; a NULL visibility verdict masks (fail closed)
    assert "NOT COALESCE((DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(l.KEY_NAME) = 'ALFA'), FALSE)" in alfa
    trexis = _sql("Trexis")
    assert "f.COMPANY = 'Trexis'" in trexis and "(other-company databases)" in trexis
    unknown = _sql("UNKNOWN")        # database_visibility_clause is '' for UNKNOWN: the user arm only
    assert "(other-company users)" in unknown and "(other-company databases)" not in unknown


def test_validation():
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            _B(bad, _D.isoformat())
    for junk in ("yesterday", "2026-13-01", "", None):
        with pytest.raises(ValueError):
            _B("WH_X", junk)
    # a date / Timestamp / datetime string all normalize to the same literal
    for day in (_D, pd.Timestamp(_D), "2026-09-20 00:00:00"):
        assert "x.DAY <= '2026-09-20'::DATE" in _B("WH_X", day)


def _clamps(sql: str) -> tuple[int, int]:
    lo = date.fromisoformat(re.search(r"x\.DAY >= '(\d{4}-\d{2}-\d{2})'::DATE", sql).group(1))
    k = int(re.search(r"IFF\(r\.RN <= (\d+),", sql).group(1))
    return (_D - lo).days, k


def test_clamps():
    assert _clamps(_sql(baseline_days=999, top_n=999)) == (28, 60)
    assert _clamps(_sql(baseline_days=1, top_n=1)) == (7, 5)
    assert _clamps(_sql()) == (14, 40)


def test_worst_case_row_bound_is_under_the_row_cap():
    """<= 2*(k+1)*(n+1) USER/DATABASE rows (k named keys + the '(all other ...)' bucket per day) +
    (n+1) METERED + (n+1) SPINE rows. Derived from the RENDERED clamps, so widening a clamp without
    re-checking DEFAULT_MAX_ROWS fails here."""
    n, k = _clamps(_sql(baseline_days=10**6, top_n=10**6))
    assert 2 * (k + 1) * (n + 1) + 2 * (n + 1) < DEFAULT_MAX_ROWS
    assert (n, k) == (28, 60)
    assert DEFAULT_MAX_ROWS > 3_596 == 2 * (60 + 1) * (28 + 1) + 2 * (28 + 1)


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_hostile_warehouse_stays_inside_a_literal(payload):
    for company in ("ALL", "ALFA"):
        assert not _escapes_a_literal(_B(payload, _D.isoformat(), company))


def test_canary_registered_on_a_recent_anchor_and_not_an_expected_gap():
    from app.data.canary import CANARIES, EXPECTED_GAPS
    reg = dict(CANARIES)
    assert "mart27.alloc_xdim_day_drivers" in reg
    assert "mart27.alloc_xdim_day_drivers" not in EXPECTED_GAPS
    sql = reg["mart27.alloc_xdim_day_drivers"]()
    assert "WH_ALFA_ADMIN" in sql and "f.COMPANY = 'ALFA'" in sql
    src = (Path(__file__).resolve().parents[1] / "app" / "data" / "canary.py").read_text(encoding="utf-8")
    line = src.split('"mart27.alloc_xdim_day_drivers"', 1)[1].split("\n    (", 1)[0]
    assert "account_today() - timedelta(days=1)" in line          # recent anchor, never a fixed date


# ---------------------------------------------------------------------------
# Executed harness: the real SQL in sqlite, through the explainer
# ---------------------------------------------------------------------------

def _to_sqlite(sql: str) -> str:
    sql = re.sub(r"\bDBA_MAINT_DB\.OVERWATCH\.", "", sql)
    sql = re.sub(r"('\d{4}-\d{2}-\d{2}')::DATE", r"\1", sql)
    sql = sql.replace("FALSE", "0")
    assert "::" not in sql and "DBA_MAINT_DB" not in sql
    return sql


_USERS = {"A": "ALFA", "B": "Trexis", "C": "ALFA"}


def _db(seed_extra_users: int = 0) -> sqlite3.Connection:
    """15 loaded days of WH_A (A $100 on even offsets, B odd — B is a Trexis user — C $300 on
    offsets 3/7/11 and D) plus a quiet WH_OTHER that keeps every day in the spine; METERED =
    allocated + idle ($20/day, $60 on D). ``seed_extra_users`` adds small steady users U0..Un."""
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("COMPANY_FOR_WAREHOUSE", 1, lambda w: "ALFA")
    con.create_function("COMPANY_FOR_USER", 1, lambda u: _USERS.get(u, "ALFA"))
    con.execute("CREATE TABLE FACT_COST_ALLOC_XDIM_DAILY (DAY TEXT, WAREHOUSE_NAME TEXT, "
                "DATABASE_NAME TEXT, USER_NAME TEXT, EXEC_SEC REAL, ALLOC_CREDITS REAL)")
    con.execute("CREATE TABLE FACT_WAREHOUSE_DAILY (DAY TEXT, WAREHOUSE_NAME TEXT, COMPANY TEXT, "
                "CREDITS_COMPUTE REAL, CREDITS_TOTAL REAL)")
    xdim, met = [], []
    for o in range(20, -1, -1):                       # 20 days loaded; the reader asks for 14
        d = (_D - timedelta(days=o)).isoformat()
        alloc = 100.0
        xdim.append((d, "WH_A", "DB_SALES", "A" if o % 2 == 0 else "B", 1.0, 100.0))
        if o in (0, 3, 7, 11):
            xdim.append((d, "WH_A", "DB_ADHOC", "C", 1.0, 300.0))
            alloc += 300.0
        for i in range(seed_extra_users):
            usd = 1.0 + (i if o == 0 else 0)
            xdim.append((d, "WH_A", f"DB_U{i}", f"U{i}", 1.0, usd))
            alloc += usd
        xdim.append((d, "WH_OTHER", "DB_X", "Z", 1.0, 5.0))
        met.append((d, "WH_A", "ALFA", 0.0, alloc + (60.0 if o == 0 else 20.0)))
    con.executemany("INSERT INTO FACT_COST_ALLOC_XDIM_DAILY VALUES (?,?,?,?,?,?)", xdim)
    con.executemany("INSERT INTO FACT_WAREHOUSE_DAILY VALUES (?,?,?,?,?)", met)
    # one day the XDIM loader skipped for EVERY warehouse (its metering row stays): it must drop out
    # of the spine and of METERED alike
    con.execute("DELETE FROM FACT_COST_ALLOC_XDIM_DAILY WHERE DAY = ?", ((_D - timedelta(days=5)).isoformat(),))
    return con


def _explain(con, company="ALL", *, max_rows=10, **kw):
    df = pd.read_sql_query(_to_sqlite(_B("WH_A", _D.isoformat(), company, **kw)), con)
    df["USD"] = df["CREDITS"] * 3.68
    return df, explain_below_warehouse(df, _D, "WH_A", max_rows=max_rows)


def test_harness_all_scope_splits_and_adds_up():
    df, exp = _explain(_db())
    spine = sorted(df.loc[df["DIMENSION"] == "SPINE", "DAY"])
    assert len(spine) == 14 and (_D - timedelta(days=5)).isoformat() not in spine   # 15 asked, 1 skipped
    assert (_D - timedelta(days=5)).isoformat() not in set(df.loc[df["DIMENSION"] == "METERED", "DAY"])
    assert exp.ok and exp.baseline_days == 13
    assert exp.by_user[0].name == "C"
    for table in (exp.by_user, exp.by_database):
        assert abs(sum(r.delta_usd for r in table) - exp.metered_delta_usd) < 0.01
    # idle went $20 -> $60 at $3.68/credit
    assert exp.unallocated_delta_usd == round(40 * 3.68, 2)
    assert _explain(_db(), "ALL")[1].by_user == exp.by_user              # deterministic


def test_harness_company_scope_masks_without_losing_dollars():
    _, alfa = _explain(_db(), "ALFA")
    _, every = _explain(_db(), "ALL")
    names = {r.name for r in alfa.by_user}
    assert "(other-company users)" in names and "B" not in names          # B is a Trexis user
    assert alfa.metered_delta_usd == every.metered_delta_usd
    assert abs(sum(r.delta_usd for r in alfa.by_user) - alfa.metered_delta_usd) < 0.01
    assert _explain(_db(), "Trexis")[1].reason.startswith("No query-allocated rows")   # f.COMPANY scoped out


def test_harness_top_n_bucket_keeps_the_sums_exact():
    con = _db(seed_extra_users=12)
    df, exp = _explain(con, top_n=5, max_rows=3)
    users = set(df.loc[df["DIMENSION"] == "USER", "KEY_NAME"])
    assert "(all other users)" in users and len(users) == 6              # 5 named + the bucket
    assert "C" in users                                                   # the mover survives the rank
    names = [r.name for r in exp.by_user]
    assert names[0] == "C" and "All other users (2+)" in names           # 2 named + the SQL bucket
    assert "(all other users)" not in names
    for table in (exp.by_user, exp.by_database):
        assert abs(sum(r.delta_usd for r in table) - exp.metered_delta_usd) < 0.01
        assert table[-1].name == UNALLOCATED_LABEL
