"""Next-Fifty #27 — mart27_sql.alloc_xdim_day_drivers, the below-warehouse day-drill reader.

String/shape locks (parse, facts only, window literals, exact warehouse, company scope + masking,
validation, clamps, injection, canary, the row bound) plus an EXECUTED sqlite harness: the real SQL
runs over seeded FACT_COST_ALLOC_XDIM_DAILY / FACT_WAREHOUSE_DAILY rows and through
anomaly_explain.explain_below_warehouse, proving the spine, the top-N bucket (ranked over the LOADED days)
and the company masks keep every table adding up to the warehouse's metered move.
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
    # the |delta| rank keeps the movers in the top N — divided by the LOADED baseline days (the
    # explainer's own divisor), never the nominal window (F17: '/ 14)' gave a steady key a phantom
    # delta after a loader gap and pushed the real mover into '(all other users)')
    assert "nb AS (\n    SELECT COUNT(*) AS N_BASE FROM spine WHERE DAY < '2026-09-20'::DATE\n)" in sql
    assert "PARTITION BY m.DIMENSION ORDER BY" in sql
    assert "/ NULLIF(MAX(nb.N_BASE), 0)) DESC,\n               m.KEY_NAME) AS RN" in sql
    assert "FROM masked m CROSS JOIN nb" in sql
    assert "/ 14)" not in sql and "/ {n}" not in sql
    ranked = sql.split("ranked AS (", 1)[1].split("\n)\n", 1)[0]
    assert "(SELECT" not in ranked                                     # no scalar subquery in the window
    assert "IFF(r.RN <= 40, m.KEY_NAME," in sql


_UNK_ARM = ("WHEN l.DIMENSION = 'USER' AND COALESCE((DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(l.KEY_NAME) "
            "= 'UNKNOWN'), TRUE) THEN '(unclassified users)'")


def test_company_scope_and_masking():
    sql_all = _sql("ALL")
    for token in ("COMPANY_FOR_WAREHOUSE", "COMPANY_FOR_USER", "other-company", "outside", "unclassified",
                  "CASE", "f.COMPANY"):
        assert token not in sql_all, token                             # ALL masks nothing
    alfa = _sql("ALFA")
    assert "DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_WAREHOUSE(x.WAREHOUSE_NAME) = 'ALFA'" in alfa
    assert "f.COMPANY = 'ALFA'" in alfa
    assert "THEN '(users outside ALFA)'" in alfa and "THEN '(databases outside ALFA)'" in alfa
    assert "other-company" not in alfa
    # masked, never dropped; a NULL visibility verdict masks (fail closed)
    assert "NOT COALESCE((DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(l.KEY_NAME) = 'ALFA'), FALSE)" in alfa
    # F16: an UNKNOWN-classified user (task / service login) is NOT another company's user — its own
    # arm, evaluated BEFORE the outside-company arm (first matching WHEN wins); a NULL verdict too
    assert _UNK_ARM in alfa
    assert alfa.index(_UNK_ARM) < alfa.index("THEN '(users outside ALFA)'")
    trexis = _sql("Trexis")
    assert "f.COMPANY = 'Trexis'" in trexis and "THEN '(databases outside Trexis)'" in trexis
    assert _UNK_ARM in trexis and trexis.index(_UNK_ARM) < trexis.index("THEN '(users outside Trexis)'")
    # UNKNOWN: its unclassified users ARE the scope (no unclassified arm); database_visibility_clause
    # is '' for UNKNOWN, so the user arm only
    unknown = _sql("UNKNOWN")
    assert "THEN '(users outside UNKNOWN)'" in unknown
    assert "unclassified" not in unknown and "databases outside" not in unknown
    assert "NOT COALESCE((DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_USER(l.KEY_NAME) = 'UNKNOWN'), FALSE)" in unknown


@pytest.mark.parametrize("company", ["ALL", "ALFA", "Trexis", "UNKNOWN"])
def test_every_label_the_reader_emits_is_a_bucket_row(company):
    """One source of truth: every synthetic KEY_NAME the SQL can write (masks + the top-N fold) is
    detected by anomaly_explain.is_bucket_row by exact string — a relabel on either side fails here
    instead of silently naming '(users outside ALFA)' as a user in the narrative."""
    from app.logic.anomaly_explain import is_bucket_row
    sql = _sql(company)
    labels = set(re.findall(r"'(\([^']*\))'", sql))
    assert {"(all other users)", "(all other databases)"} <= labels
    expected = {"ALL": set(), "UNKNOWN": {"(users outside UNKNOWN)"},
                "ALFA": {"(unclassified users)", "(users outside ALFA)", "(databases outside ALFA)"},
                "Trexis": {"(unclassified users)", "(users outside Trexis)", "(databases outside Trexis)"}}
    assert labels - {"(all other users)", "(all other databases)"} == expected[company]
    for label in labels:
        assert is_bucket_row(label), label


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
    sql = sql.replace("FALSE", "0").replace("TRUE", "1")
    assert "::" not in sql and "DBA_MAINT_DB" not in sql
    return sql


# S is a service login with no company role: COMPANY_FOR_USER classifies it 'UNKNOWN' (V044).
_USERS = {"A": "ALFA", "B": "Trexis", "C": "ALFA", "S": "UNKNOWN"}


def _db(seed_extra_users: int = 0, *, service_user: bool = False, wh_company: str = "ALFA") -> sqlite3.Connection:
    """15 loaded days of WH_A (A $100 on even offsets, B odd — B is a Trexis user — C $300 on
    offsets 3/7/11 and D) plus a quiet WH_OTHER that keeps every day in the spine; METERED =
    allocated + idle ($20/day, $60 on D). ``seed_extra_users`` adds small steady users U0..Un;
    ``service_user`` adds S (unclassified: 2 credits a day on DB_ETL, 500 on D); ``wh_company`` is
    WH_A's COMPANY_FOR_WAREHOUSE / FACT_WAREHOUSE_DAILY.COMPANY."""
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.create_function("COMPANY_FOR_WAREHOUSE", 1, lambda w: wh_company)
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
        if service_user:
            usd = 500.0 if o == 0 else 2.0
            xdim.append((d, "WH_A", "DB_ETL", "S", 1.0, usd))
            alloc += usd
        xdim.append((d, "WH_OTHER", "DB_X", "Z", 1.0, 5.0))
        met.append((d, "WH_A", wh_company, 0.0, alloc + (60.0 if o == 0 else 20.0)))
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
    assert "(users outside ALFA)" in names and "B" not in names           # B is a Trexis user
    assert alfa.metered_delta_usd == every.metered_delta_usd
    assert abs(sum(r.delta_usd for r in alfa.by_user) - alfa.metered_delta_usd) < 0.01
    assert _explain(_db(), "Trexis")[1].reason.startswith("No query-allocated rows")   # f.COMPANY scoped out


def test_harness_unclassified_service_login_is_its_own_row_and_leads_the_narrative():
    """F16: S (COMPANY_FOR_USER = 'UNKNOWN', a service login) drives the day. Under ALFA it is
    '(unclassified users)' — not '(users outside ALFA)', which holds only Trexis's B — and, as the
    largest mover, it leads the 'By user' clause ahead of the largest named user."""
    df, alfa = _explain(_db(service_user=True), "ALFA")
    users = set(df.loc[df["DIMENSION"] == "USER", "KEY_NAME"])
    assert {"(unclassified users)", "(users outside ALFA)"} <= users and not {"S", "B"} & users
    rows = {r.name: r for r in alfa.by_user}
    assert rows["(users outside ALFA)"].actual_usd == 0.0                 # B runs on odd offsets only
    unc = rows["(unclassified users)"]
    assert unc.delta_usd == round((500.0 - 2.0) * 3.68, 2)
    assert alfa.by_user[0].name == "(unclassified users)"
    assert ("By user: the largest change is in unclassified users — $1,832.64 over their average"
            in alfa.narrative)
    assert "; the largest named user is C $" in alfa.narrative
    assert abs(sum(r.delta_usd for r in alfa.by_user) - alfa.metered_delta_usd) < 0.01
    # ALL masks nothing: S is named
    _, every = _explain(_db(service_user=True), "ALL")
    assert every.by_user[0].name == "S" and "By user: S $1,832.64 over its average" in every.narrative
    assert every.metered_delta_usd == alfa.metered_delta_usd


def test_harness_unknown_scope_keeps_its_own_unclassified_users_by_name():
    """Under the UNKNOWN scope the unclassified users ARE the scope: S stays named, and only the
    users classified to a company fold into '(users outside UNKNOWN)'."""
    df, unk = _explain(_db(service_user=True, wh_company="UNKNOWN"), "UNKNOWN")
    users = set(df.loc[df["DIMENSION"] == "USER", "KEY_NAME"])
    assert "S" in users and "(users outside UNKNOWN)" in users
    assert not {"A", "B", "C", "(unclassified users)"} & users
    assert unk.ok and unk.by_user[0].name == "S"
    assert "By user: S $1,832.64 over its average" in unk.narrative
    assert abs(sum(r.delta_usd for r in unk.by_user) - unk.metered_delta_usd) < 0.01


def test_harness_rank_survives_a_loader_gap():
    """F17: 7 of the 14 baseline days were never loaded, 45 users run a steady 30 credits every
    loaded day, and MOVER appears on D with 12. Ranking by the nominal window (/ 14) gave each steady
    user a phantom |30 - 210/14| = 15 > 12 and folded MOVER into '(all other users)'; ranking by the
    loaded days (the explainer's divisor) keeps it, and the narrative names it."""
    con = sqlite3.connect(":memory:")
    con.create_function("IFF", 3, lambda c, a, b: a if c else b)
    con.execute("CREATE TABLE FACT_COST_ALLOC_XDIM_DAILY (DAY TEXT, WAREHOUSE_NAME TEXT, "
                "DATABASE_NAME TEXT, USER_NAME TEXT, EXEC_SEC REAL, ALLOC_CREDITS REAL)")
    con.execute("CREATE TABLE FACT_WAREHOUSE_DAILY (DAY TEXT, WAREHOUSE_NAME TEXT, COMPANY TEXT, "
                "CREDITS_COMPUTE REAL, CREDITS_TOTAL REAL)")
    xdim, met = [], []
    for o in range(14, -1, -1):
        d = (_D - timedelta(days=o)).isoformat()
        alloc = 0.0
        if o == 0 or o % 2 == 1:                                      # offsets 2,4,..,14 never loaded
            for i in range(45):
                xdim.append((d, "WH_A", f"DB_S{i:02d}", f"STEADY{i:02d}", 1.0, 30.0))
                alloc += 30.0
            if o == 0:
                xdim.append((d, "WH_A", "DB_NEW", "MOVER", 1.0, 12.0))
                alloc += 12.0
        met.append((d, "WH_A", "ALFA", 0.0, alloc))
    con.executemany("INSERT INTO FACT_COST_ALLOC_XDIM_DAILY VALUES (?,?,?,?,?,?)", xdim)
    con.executemany("INSERT INTO FACT_WAREHOUSE_DAILY VALUES (?,?,?,?,?)", met)
    df, exp = _explain(con, max_rows=6)
    assert len(df[df["DIMENSION"] == "SPINE"]) == 8                      # 7 baseline days + D
    users = set(df.loc[df["DIMENSION"] == "USER", "KEY_NAME"])
    assert "MOVER" in users and "(all other users)" in users            # 46 keys > top_n 40
    assert "DB_NEW" in set(df.loc[df["DIMENSION"] == "DATABASE", "KEY_NAME"])
    assert exp.ok and exp.baseline_days == 7
    assert exp.by_user[0].name == "MOVER" and exp.by_user[0].delta_usd == round(12 * 3.68, 2)
    assert "By user: MOVER $44.16 over its average" in exp.narrative
    assert "By database: DB_NEW $44.16 over its average" in exp.narrative
    for table in (exp.by_user, exp.by_database):
        assert abs(sum(r.delta_usd for r in table) - exp.metered_delta_usd) < 0.01


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
