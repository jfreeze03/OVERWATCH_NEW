"""Executed harness for Next-Fifty #31 (revert detection + Proof "Saved to date"): the REAL
mart_sql.savings_summary_quarter / ledger_attribution / savings_ledger(limit=None) SQL runs in sqlite over a
seeded SAVINGS_LEDGER + WAREHOUSE_CHANGE_REGISTRY (+ SETTINGS, REMEDIATION_LOG, ALERT_EVENTS), modelled on
tests/test_cs_billed_families_harness.py.

Translation: table FQNs stripped, account_today_sql() pinned to TODAY, `::TIMESTAMP_NTZ` dropped (the seed
stores naive account time) and `x::DATE` rewritten to sqlite date(x); DATEADD / DATEDIFF / DATE_TRUNC /
IFF / COUNT_IF / TRY_TO_DOUBLE / STARTSWITH / CONTAINS / SPLIT_PART shimmed, and LEAST / GREATEST with
Snowflake's NULL semantics (ANY NULL argument -> NULL: the trap the Saved-to-date COALESCE guards against).
QUALIFY is rewritten to a filtered subquery, kept inside its own CTE and its own (nested) SELECT: the
revert CTE's per-change derived table carries its own QUALIFY inside the co-attributed group's.

Locks the arithmetic the string locks in tests/test_ledger_reverts.py cannot: which booked changes a later
change reverts (full / partial / not at all, carried across a co-attributed group), that the ROI numerator and the attribution split drop the SAME
rows (one predicate), that the pandas fallback (actions.ledger_totals over the executed ledger read) agrees
with the SQL, and the Saved-to-date accrual (start, 12-month stop, revert stop, measured vs carried).
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

import app.logic.actions as actions_mod
from app.config import CORE_SCHEMA, OVERWATCH_DB
from app.data import mart_sql
from app.data.common import account_today_sql
from app.logic import proof
from app.logic.actions import ledger_totals

TODAY = "2026-09-28"


# --------------------------------------------------------------------------- Snowflake -> sqlite
def _wrap_casts(sql: str, cast: str) -> str:
    """Rewrite every `<operand><cast>` as date(<operand>): the operand is an identifier chain (l.X) or a
    parenthesised call (COALESCE(...)) including its function name."""
    out = sql
    while cast in out:
        i = out.index(cast)
        j = i
        if out[j - 1] == ")":
            depth = 0
            while True:
                j -= 1
                if out[j] == ")":
                    depth += 1
                elif out[j] == "(":
                    depth -= 1
                    if depth == 0:
                        break
        while j > 0 and (out[j - 1].isalnum() or out[j - 1] in "._"):
            j -= 1
        out = f"{out[:j]}date({out[j:i]}){out[i + len(cast):]}"
    return out


def _depths(sql: str) -> list[int]:
    """Parenthesis depth at every character, outside single-quoted literals (a quote char keeps its depth)."""
    out, depth, quoted = [], 0, False
    for ch in sql:
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch == "(":
            out.append(depth)
            depth += 1
            continue
        elif not quoted and ch == ")":
            depth -= 1
        out.append(depth)
    return out


def _word_at(sql: str, i: int, word: str) -> bool:
    return (sql.startswith(word, i) and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_"))
            and not (sql[i + len(word)].isalnum() or sql[i + len(word)] == "_"))


def _rewrite_first_qualify(sql: str) -> str:
    """The FIRST `QUALIFY ROW_NUMBER() OVER (...) = 1` -> `SELECT * FROM (SELECT <cols>, <win> AS _RN FROM ...)
    WHERE _RN = 1`. Its owning SELECT is the nearest one at the same parenthesis depth without leaving the
    scope (so a QUALIFY never reaches into an earlier CTE, and an outer select skips a nested derived table).
    The first QUALIFY in the text is always the innermost: an outer select's QUALIFY follows its FROM."""
    q = sql.index("QUALIFY ")
    depth = _depths(sql)
    dq = depth[q]
    owner = None
    for i in range(q - 1, -1, -1):
        if depth[i] < dq:
            break
        if depth[i] == dq and _word_at(sql, i, "SELECT"):
            owner = i
            break
    assert owner is not None, sql[max(0, q - 200):q]
    frm = next(i for i in range(owner, q) if depth[i] == dq and _word_at(sql, i, "FROM"))
    win_start = q + len("QUALIFY ")
    assert sql.startswith("ROW_NUMBER() OVER (", win_start), sql[q:q + 80]
    open_paren = win_start + len("ROW_NUMBER() OVER ")
    close = next(i for i in range(open_paren + 1, len(sql)) if sql[i] == ")" and depth[i] == dq)
    assert sql.startswith(" = 1", close + 1), sql[q:close + 10]
    cols = sql[owner + len("SELECT "):frm].rstrip()
    rest = sql[frm:q].rstrip()
    win = sql[win_start:close + 1]
    return f"{sql[:owner]}SELECT * FROM (SELECT {cols}, {win} AS _RN\n{rest}\n) WHERE _RN = 1{sql[close + 5:]}"


def _to_sqlite(sql: str) -> str:
    sql = sql.replace(account_today_sql(), f"'{TODAY}'")
    sql = sql.replace(f"{OVERWATCH_DB}.{CORE_SCHEMA}.", "")
    sql = sql.replace("::TIMESTAMP_NTZ", "")
    sql = _wrap_casts(sql, "::DATE")
    while "QUALIFY " in sql:
        sql = _rewrite_first_qualify(sql)
    assert "QUALIFY" not in sql and "::" not in sql and "CURRENT_" not in sql and "DBA_MAINT_DB" not in sql
    return sql


def _parse(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value)
    return datetime.fromisoformat(text if len(text) > 10 else text + " 00:00:00")


def _fmt(value: datetime, as_date: bool) -> str:
    return value.date().isoformat() if as_date else value.isoformat(sep=" ")


def _dateadd(unit, n, value):
    ts = _parse(value)
    if ts is None:
        return None
    n = int(n)
    if unit == "month":
        month = ts.month - 1 + n
        year, month = ts.year + month // 12, month % 12 + 1
        ts = ts.replace(year=year, month=month, day=min(ts.day, calendar.monthrange(year, month)[1]))
    elif unit == "day":
        ts = ts + timedelta(days=n)
    elif unit == "hour":
        ts = ts + timedelta(hours=n)
    else:
        raise AssertionError(f"DATEADD unit {unit!r} not shimmed")
    return _fmt(ts, len(str(value)) <= 10)


def _datediff(unit, a, b):
    assert unit == "day", unit
    if a is None or b is None:
        return None
    return (_parse(b).date() - _parse(a).date()).days


def _date_trunc(unit, value):
    assert unit == "quarter", unit
    d = _parse(value).date()
    return date(d.year, (d.month - 1) // 3 * 3 + 1, 1).isoformat()


def _least(*a):
    return None if any(x is None for x in a) else min(a)


def _greatest(*a):
    return None if any(x is None for x in a) else max(a)


def _try_double(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _split_part(s, d, i):
    if s is None:
        return None
    parts = str(s).split(d)
    return parts[i - 1] if 0 < i <= len(parts) else ""


class _CountIf:
    def __init__(self):
        self.n = 0

    def step(self, c):
        if c:
            self.n += 1

    def finalize(self):
        return self.n


_SCHEMA = """
CREATE TABLE SAVINGS_LEDGER(ITEM_ID TEXT, ACTION_ID TEXT, CREATED_AT TEXT, DESCRIPTION TEXT, STATE TEXT,
    ESTIMATED_USD REAL, VERIFIED_USD REAL, VERIFIED_AT TEXT, VERIFIED_BY TEXT, PROOF_SQL TEXT, NOTES TEXT,
    FINDING_TYPE TEXT, TARGET_OBJECT TEXT, SOURCE_CHANGE_ID TEXT);
CREATE TABLE WAREHOUSE_CHANGE_REGISTRY(CHANGE_ID TEXT, WAREHOUSE_NAME TEXT, COMPANY TEXT, SETTING TEXT,
    OLD_VALUE TEXT, NEW_VALUE TEXT, CHANGE_SEEN_AT TEXT, BASELINE_QUERIES REAL, BASELINE_CREDITS_PER_DAY REAL,
    AFTER_DAYS REAL, AFTER_QUERIES REAL, AFTER_CREDITS_PER_DAY REAL, VERDICT TEXT, TRACKING_UNTIL TEXT,
    CHANGED_BY TEXT);
CREATE TABLE SETTINGS(KEY TEXT, VALUE TEXT);
CREATE TABLE REMEDIATION_LOG(REMEDIATION_ID TEXT, FINDING_TYPE TEXT, STATEMENT_SQL TEXT, STATUS TEXT,
    EST_MONTHLY_SAVINGS_USD REAL, EXECUTED_AT TEXT, EXECUTED_BY TEXT);
CREATE TABLE ALERT_EVENTS(EVENT_ID TEXT, RULE_ID TEXT, DEDUPE_KEY TEXT, METRIC_VALUE REAL, RAISED_AT TEXT);
"""


@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.create_function("IFF", 3, lambda cond, a, b: a if cond else b)
    c.create_function("TRY_TO_DOUBLE", 1, _try_double)
    c.create_function("DATEADD", 3, _dateadd)
    c.create_function("DATEDIFF", 3, _datediff)
    c.create_function("DATE_TRUNC", 2, _date_trunc)
    c.create_function("LEAST", -1, _least)
    c.create_function("GREATEST", -1, _greatest)
    c.create_function("STARTSWITH", 2, lambda a, b: None if a is None or b is None else a.startswith(b))
    c.create_function("CONTAINS", 2, lambda a, b: None if a is None or b is None else b in a)
    c.create_function("SPLIT_PART", 3, _split_part)
    c.create_aggregate("COUNT_IF", 1, _CountIf)
    c.executescript(_SCHEMA)
    c.execute("INSERT INTO SETTINGS VALUES ('CREDIT_PRICE_USD', '3.68')")
    yield c
    c.close()


def _reg(c, cid, wh, setting, old, new, seen, *, after_days=14.0, verdict="IMPROVED", until=None):
    c.execute("INSERT INTO WAREHOUSE_CHANGE_REGISTRY VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (cid, wh, "ALFA", setting, old, new, seen, 400.0, 10.0, after_days, 400.0, 8.0, verdict,
               until or seen[:10], None))


def _led(c, item, state, verified_usd, verified_at, *, source=None, estimated=0.0, created=None,
         finding=None, target=None, notes="Auto-booked | measured on the full window"):
    c.execute("INSERT INTO SAVINGS_LEDGER VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (item, None, created or (verified_at or "2026-06-01 06:45:00"), f"item {item}", state, estimated,
               verified_usd, verified_at, "AUTO", "SELECT 1", notes, finding, target, source))


def _run(c, sql: str) -> pd.DataFrame:
    cur = c.execute(_to_sqlite(sql))
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def _summary(c) -> pd.Series:
    return _run(c, mart_sql.savings_summary_quarter()).iloc[0]


def _ledger(c) -> pd.DataFrame:
    return _run(c, mart_sql.savings_ledger(limit=None))


def test_translator_keeps_qualify_inside_its_cte():
    for sql in (mart_sql.savings_summary_quarter(), mart_sql.ledger_attribution(),
                mart_sql.savings_ledger(limit=None)):
        out = _to_sqlite(sql)
        assert out.count("AS _RN") == sql.count("QUALIFY ROW_NUMBER()")
    # the attribution's four QUALIFY CTEs (twin, rv, rem, rec) each keep their own window
    attr = _to_sqlite(mart_sql.ledger_attribution())
    rem = attr.split("rem AS (", 1)[1].split("\n),", 1)[0]
    assert "PARTITION BY r.CHANGE_ID ORDER BY rl.EXECUTED_AT DESC) AS _RN" in rem


# --------------------------------------------------------------------------- the revert matrix
def _seed_matrix(c) -> None:
    # A: LARGE -> SMALL booked; later SMALL -> MEDIUM: costlier than the booked NEW, below the OLD -> partial
    _reg(c, "A", "WH_A", "SIZE", "Large", "Small", "2026-06-01 06:40:00")
    _reg(c, "A2", "WH_A", "SIZE", "Small", "Medium", "2026-08-01 06:40:00")
    _led(c, "lA", "VERIFIED", 300.0, "2026-06-16 06:45:00", source="A", finding="RESIZE")
    # B: 600 -> 60 booked; later 60 -> 0 (never suspends: the costliest) -> full
    _reg(c, "B", "WH_B", "AUTO_SUSPEND", "600", "60", "2026-06-01 06:40:00")
    _reg(c, "B2", "WH_B", "AUTO_SUSPEND", "60", "0", "2026-09-01 06:40:00")
    _led(c, "lB", "VERIFIED", 90.0, "2026-06-16 06:45:00", source="B", finding="AUTO_SUSPEND")
    # BN: 600 -> 60 booked; later 60 -> NULL (SHOW's "never") -> full
    _reg(c, "BN", "WH_BN", "AUTO_SUSPEND", "600", "60", "2026-05-01 06:40:00")
    _reg(c, "BN2", "WH_BN", "AUTO_SUSPEND", "60", None, "2026-07-15 06:40:00")
    _led(c, "lBN", "VERIFIED", 45.0, "2026-05-16 06:45:00", source="BN", finding="AUTO_SUSPEND")
    # C/D chain: X-Large -> Small (C), Small -> X-Small (D), then X-Small -> Small: D reverted (full),
    # C untouched (Small is not costlier than C's NEW)
    _reg(c, "C", "WH_C", "SIZE", "X-Large", "Small", "2026-05-01 06:40:00")
    _reg(c, "D", "WH_C", "SIZE", "Small", "X-Small", "2026-06-01 06:40:00")
    _reg(c, "D2", "WH_C", "SIZE", "X-Small", "Small", "2026-07-01 06:40:00")
    _led(c, "lC", "VERIFIED", 600.0, "2026-05-16 06:45:00", source="C", finding="RESIZE")
    _led(c, "lD", "VERIFIED", 60.0, "2026-06-16 06:45:00", source="D", finding="RESIZE")
    # E: SCALING_POLICY STANDARD -> ECONOMY, later back to STANDARD; the adopted row is still ESTIMATED
    # with an up-front estimate -> it leaves ESTIMATED_OPEN_USD
    _reg(c, "E", "WH_E", "SCALING_POLICY", "STANDARD", "ECONOMY", "2026-06-01 06:40:00")
    _reg(c, "E2", "WH_E", "SCALING_POLICY", "ECONOMY", "STANDARD", "2026-06-20 06:40:00")
    _led(c, "lE", "ESTIMATED", None, None, source="E", estimated=25.0, finding="SCALING_POLICY",
         created="2026-06-01 06:45:00", notes="booked | adopted by the daily change scan (change E)")
    # F: MAX_CLUSTERS 4 -> 2; a later MIN_CLUSTERS change on the same warehouse is never a revert
    _reg(c, "F", "WH_F", "MAX_CLUSTERS", "4", "2", "2026-06-01 06:40:00")
    _reg(c, "F2", "WH_F", "MIN_CLUSTERS", "1", "2", "2026-07-01 06:40:00")
    _led(c, "lF", "VERIFIED", 30.0, "2026-06-16 06:45:00", source="F", finding="MAX_CLUSTERS")
    # G: 600 -> 60, later tightened 60 -> 30: not a revert
    _reg(c, "G", "WH_G", "AUTO_SUSPEND", "600", "60", "2026-06-01 06:40:00")
    _reg(c, "G2", "WH_G", "AUTO_SUSPEND", "60", "30", "2026-07-01 06:40:00")
    _led(c, "lG", "VERIFIED", 45.0, "2026-06-16 06:45:00", source="G", finding="AUTO_SUSPEND")
    # GA 600 -> 60, GB 60 -> 30, then 30 -> 60: only GB (60 is not costlier than GA's NEW 60)
    _reg(c, "GA", "WH_G2", "AUTO_SUSPEND", "600", "60", "2026-04-01 06:40:00")
    _reg(c, "GB", "WH_G2", "AUTO_SUSPEND", "60", "30", "2026-05-01 06:40:00")
    _reg(c, "GC", "WH_G2", "AUTO_SUSPEND", "30", "60", "2026-06-01 06:40:00")
    _led(c, "lGA", "VERIFIED", 50.0, "2026-04-16 06:45:00", source="GA", finding="AUTO_SUSPEND")
    _led(c, "lGB", "VERIFIED", 20.0, "2026-05-16 06:45:00", source="GB", finding="AUTO_SUSPEND")
    # H: an EARLIER size-up before the booked size-down is ignored
    _reg(c, "H0", "WH_H", "SIZE", "Medium", "Large", "2026-04-01 06:40:00")
    _reg(c, "H", "WH_H", "SIZE", "Large", "Medium", "2026-05-01 06:40:00")
    _led(c, "lH", "VERIFIED", 120.0, "2026-05-16 06:45:00", source="H", finding="RESIZE")
    # M: a manual (app-booked) row on a reverted warehouse is never revert-checked
    _led(c, "lM", "VERIFIED", 40.0, "2026-06-10 10:00:00", finding="SCHEDULE", target="WH_A",
         notes="booked from Optimize")
    # J: an ADOPTED manual row (SOURCE_CHANGE_ID stamped by V153) IS checked: 300 -> 60, later back to 300
    _reg(c, "J", "WH_J", "AUTO_SUSPEND", "300", "60", "2026-07-01 06:40:00")
    _reg(c, "J2", "WH_J", "AUTO_SUSPEND", "60", "300", "2026-08-15 06:40:00")
    _led(c, "lJ", "VERIFIED", 70.0, "2026-07-16 06:45:00", source="J", estimated=80.0, finding="AUTO_SUSPEND",
         created="2026-07-01 06:00:00", notes="booked | adopted by the daily change scan (change J)")
    # K: a manual twin superseded by the settled auto row stays excluded when that auto row is reverted
    _reg(c, "K", "WH_K", "SIZE", "Large", "Medium", "2026-07-01 06:40:00")
    _reg(c, "K2", "WH_K", "SIZE", "Medium", "Large", "2026-08-10 06:40:00")
    _led(c, "lK", "VERIFIED", 180.0, "2026-07-16 06:45:00", source="K", finding="RESIZE")
    _led(c, "lKm", "VERIFIED", 150.0, "2026-07-20 09:00:00", finding="RESIZE", target="WH_K",
         estimated=200.0, created="2026-06-30 10:00:00", notes="booked from Optimize")
    # L: LBA-1 -- SIZE + MAX_CLUSTERS in one measured window (one scan: the same CHANGE_SEEN_AT); the RN=1
    # SIZE row carries the saving, the clusters partner settled $0. Sizing back up reverts the RN=1 row
    # ('full', its own setting) and, carried across the group, the $0 partner ('partial': inherited).
    _reg(c, "L1", "WH_L", "SIZE", "Large", "Medium", "2026-06-01 06:40:00")
    _reg(c, "L2", "WH_L", "MAX_CLUSTERS", "3", "2", "2026-06-01 06:40:00")
    _reg(c, "L3", "WH_L", "SIZE", "Medium", "Large", "2026-07-10 06:40:00")
    _led(c, "lL1", "VERIFIED", 200.0, "2026-06-16 06:45:00", source="L1", finding="RESIZE")
    _led(c, "lL2", "VERIFIED", 0.0, "2026-06-16 06:45:00", source="L2", finding="MAX_CLUSTERS",
         notes="Auto | measured on the full window | LBA-1 co-attributed: once on L1.")
    # X: 6X-Large -> 4X-Large, later 4X-Large -> 5X-Large: detected, partial
    _reg(c, "X", "WH_X", "SIZE", "6X-Large", "4X-Large", "2026-06-01 06:40:00")
    _reg(c, "X2", "WH_X", "SIZE", "4X-Large", "5X-Large", "2026-07-01 06:40:00")
    _led(c, "lX", "VERIFIED", 500.0, "2026-06-16 06:45:00", source="X", finding="RESIZE")
    # XX: XXLarge (the 2X-Large synonym) -> Large, later Large -> 2X-Large: full
    _reg(c, "XX", "WH_XX", "SIZE", "XXLarge", "Large", "2026-06-01 06:40:00")
    _reg(c, "XX2", "WH_XX", "SIZE", "Large", "2X-Large", "2026-07-01 06:40:00")
    _led(c, "lXX", "VERIFIED", 100.0, "2026-06-16 06:45:00", source="XX", finding="RESIZE")
    # U: an unknown size spelling ranks NULL -> never a revert (fails open)
    _reg(c, "U", "WH_U", "SIZE", "Large", "Small", "2026-06-01 06:40:00")
    _reg(c, "U2", "WH_U", "SIZE", "Small", "Huge", "2026-07-01 06:40:00")
    _led(c, "lU", "VERIFIED", 10.0, "2026-06-16 06:45:00", source="U", finding="RESIZE")
    # W: an unknown booked OLD value: the revert is still caught (vs NEW) and reads 'partial', never NULL
    _reg(c, "W", "WH_W", "MAX_CLUSTERS", None, "2", "2026-06-01 06:40:00")
    _reg(c, "W2", "WH_W", "MAX_CLUSTERS", "2", "5", "2026-07-01 06:40:00")
    _led(c, "lW", "VERIFIED", 15.0, "2026-06-16 06:45:00", source="W", finding="MAX_CLUSTERS")
    # O: reverted, but verified before the active window: disclosed as reverted, never "active"
    _reg(c, "O", "WH_O", "SIZE", "Large", "Small", "2025-05-01 06:40:00")
    _reg(c, "O2", "WH_O", "SIZE", "Small", "Large", "2025-12-01 06:40:00")
    _led(c, "lO", "VERIFIED", 400.0, "2025-05-16 06:45:00", source="O", finding="RESIZE")


# item -> (REVERT_KIND or None, REVERTED_AT or None)
_EXPECTED_REVERTS = {
    "lA": ("partial", "2026-08-01 06:40:00"), "lB": ("full", "2026-09-01 06:40:00"),
    "lBN": ("full", "2026-07-15 06:40:00"), "lC": (None, None), "lD": ("full", "2026-07-01 06:40:00"),
    "lE": ("full", "2026-06-20 06:40:00"), "lF": (None, None), "lG": (None, None), "lGA": (None, None),
    "lGB": ("full", "2026-06-01 06:40:00"), "lH": (None, None), "lM": (None, None),
    "lJ": ("full", "2026-08-15 06:40:00"), "lK": ("full", "2026-08-10 06:40:00"), "lKm": (None, None),
    "lL1": ("full", "2026-07-10 06:40:00"), "lL2": ("partial", "2026-07-10 06:40:00"), "lX": ("partial", "2026-07-01 06:40:00"),
    "lXX": ("full", "2026-07-01 06:40:00"), "lU": (None, None), "lW": ("partial", "2026-07-01 06:40:00"),
    "lO": ("full", "2025-12-01 06:40:00"),
}


def test_revert_matrix_on_the_ledger_rows(db):
    _seed_matrix(db)
    led = _ledger(db).set_index("ITEM_ID")
    assert set(led.index) == set(_EXPECTED_REVERTS)
    for item, (kind, at) in _EXPECTED_REVERTS.items():
        row = led.loc[item]
        got_kind = None if pd.isna(row["REVERT_KIND"]) else row["REVERT_KIND"]
        got_at = None if pd.isna(row["REVERTED_AT"]) else row["REVERTED_AT"]
        assert (got_kind, got_at) == (kind, at), item
    # the undoing change is carried for the FLAGS / Reverted savings list
    assert (led.loc["lA", "REVERT_CHANGE_ID"], led.loc["lA", "REVERT_OLD_VALUE"],
            led.loc["lA", "REVERT_NEW_VALUE"]) == ("A2", "Small", "Medium")
    assert led.loc["lBN", "REVERT_CHANGE_ID"] == "BN2" and pd.isna(led.loc["lBN", "REVERT_NEW_VALUE"])
    # REVERT_SETTING: the row's own setting, except where the revert was inherited from a co-attributed
    # partner (lL2, MAX_CLUSTERS, undone through its group's SIZE change)
    assert led.loc["lA", "REVERT_SETTING"] == "SIZE" and led.loc["lB", "REVERT_SETTING"] == "AUTO_SUSPEND"
    assert (led.loc["lL2", "REVERT_SETTING"], led.loc["lL2", "REVERT_CHANGE_ID"],
            led.loc["lL2", "REVERT_NEW_VALUE"]) == ("SIZE", "L3", "Large")
    assert pd.isna(led.loc["lC", "REVERT_SETTING"])
    # the superseded manual twin is still superseded (no resurrection when its auto row is reverted)
    assert led.loc["lKm", "SUPERSEDED_BY_CHANGE_ID"] == "K"
    # one row per ledger item: the revert join never fans out
    assert len(_ledger(db)) == len(_EXPECTED_REVERTS)


def _active_kept_items() -> dict[str, float]:
    # VERIFIED, not a twin, verified in the last 12 months (>= 2025-09-28), not reverted
    return {"lC": 600.0, "lF": 30.0, "lG": 45.0, "lGA": 50.0, "lH": 120.0, "lM": 40.0, "lU": 10.0}


def _active_reverted_items() -> dict[str, float]:
    return {"lA": 300.0, "lB": 90.0, "lBN": 45.0, "lD": 60.0, "lGB": 20.0, "lJ": 70.0, "lK": 180.0,
            "lL1": 200.0, "lL2": 0.0, "lX": 500.0, "lXX": 100.0, "lW": 15.0}


def test_summary_and_attribution_drop_the_same_rows(db):
    _seed_matrix(db)
    s = _summary(db)
    kept, rev = _active_kept_items(), _active_reverted_items()
    assert s["VERIFIED_ACTIVE_MONTHLY_USD"] == pytest.approx(sum(kept.values()))        # 895.0
    assert s["VERIFIED_ACTIVE_ITEMS"] == len(kept)
    assert s["REVERTED_ACTIVE_ITEMS"] == len(rev)
    assert s["REVERTED_ACTIVE_USD"] == pytest.approx(sum(rev.values()))
    # this quarter (>= 2026-07-01): lJ and lK verified in July, both reverted; lKm is a twin -> nothing
    assert s["VERIFIED_QTD_USD"] == 0.0 and s["VERIFIED_ITEMS"] == 0
    # the reverted ESTIMATED row (lE, $25 estimate) leaves the open pipeline
    assert s["ESTIMATED_OPEN_USD"] == 0.0
    assert s["SUPERSEDED_ITEMS"] == 1
    attr = _run(db, mart_sql.ledger_attribution())
    first = attr.iloc[0]
    # ONE predicate: the attribution split's whole-ledger total equals the ROI numerator
    assert first["ACTIVE_USD"] == pytest.approx(s["VERIFIED_ACTIVE_MONTHLY_USD"])
    assert first["ACTIVE_ITEMS"] == s["VERIFIED_ACTIVE_ITEMS"]
    assert first["TOTAL_ITEMS"] == len(_EXPECTED_REVERTS)
    by = attr.set_index("ITEM_ID")
    assert by.loc["lM", "ATTRIBUTION"] == "OVERWATCH_BOOKED"
    assert by.loc["lJ", "ATTRIBUTION"] == "OVERWATCH_EXECUTED"          # adopted
    assert first["BOOKED_ACTIVE_USD"] == pytest.approx(40.0)
    split = proof.evidence_split(attr)
    assert split["active_usd"] == pytest.approx(sum(kept.values())) and split["active_items"] == len(kept)


def test_pandas_fallback_applies_the_same_rule(db, monkeypatch):
    _seed_matrix(db)
    monkeypatch.setattr(actions_mod, "account_now", lambda: datetime(2026, 9, 28, 9, 0))
    s = _summary(db)
    t = ledger_totals(_ledger(db))
    assert t["verified_active_usd"] == pytest.approx(s["VERIFIED_ACTIVE_MONTHLY_USD"])
    assert t["verified_active_count"] == s["VERIFIED_ACTIVE_ITEMS"]
    assert t["verified_qtd_usd"] == pytest.approx(s["VERIFIED_QTD_USD"])
    assert t["estimated_usd"] == pytest.approx(s["ESTIMATED_OPEN_USD"])
    assert t["reverted_active_count"] == s["REVERTED_ACTIVE_ITEMS"]
    assert t["reverted_active_usd"] == pytest.approx(s["REVERTED_ACTIVE_USD"])
    # lO is reverted but older than the active window: disclosed as reverted, never as active
    assert t["reverted_count"] == s["REVERTED_ACTIVE_ITEMS"] + 1
    assert t["reverted_usd"] == pytest.approx(s["REVERTED_ACTIVE_USD"] + 400.0)
    assert t["reverted_pending_count"] == 1                                   # lE
    # the estimate-accuracy figures keep reverted rows: lJ (est 80 -> 70) is the only positive estimate
    assert t["realization_pct"] == 87.5 and t["realized_estimated_usd"] == 80.0


def test_verified_wins_drops_reverted_fixes(db):
    _seed_matrix(db)
    wins = _run(db, mart_sql.verified_wins("ALL"))
    targets = set(wins["TARGET_WAREHOUSE"])
    assert {"WH_C", "WH_F", "WH_G", "WH_G2", "WH_H", "WH_U"} <= targets
    for wh in ("WH_B", "WH_BN", "WH_J", "WH_K", "WH_L", "WH_X", "WH_XX", "WH_W", "WH_O"):
        assert wh not in targets, wh
    # WH_A: the reverted auto row is gone; the manual SCHEDULE row targeting WH_A is never revert-checked
    assert list(wins.loc[wins["TARGET_WAREHOUSE"] == "WH_A", "FIX_TYPE"]) == ["SCHEDULE"]


# --------------------------------------------------------------------------- co-attributed groups (review r1 F1)
def _seed_partner_undone(c) -> None:
    # P: AUTO_SUSPEND 600 -> 60 and SIZE Large -> Medium, seen by ONE scan (the same CHANGE_SEEN_AT: one
    # measured window, the V153 LBA-1 partition). The AUTO_SUSPEND row is RN=1 and carries the $200; the
    # SIZE partner settled $0. Only the $0 partner is undone (SIZE back to Large on Jul 10).
    _reg(c, "P1", "WH_P", "AUTO_SUSPEND", "600", "60", "2026-06-01 06:40:00")
    _reg(c, "P2", "WH_P", "SIZE", "Large", "Medium", "2026-06-01 06:40:00")
    _reg(c, "P3", "WH_P", "SIZE", "Medium", "Large", "2026-07-10 06:40:00")
    _led(c, "lP1", "VERIFIED", 200.0, "2026-06-16 06:45:00", source="P1", finding="AUTO_SUSPEND")
    _led(c, "lP2", "VERIFIED", 0.0, "2026-06-16 06:45:00", source="P2", finding="RESIZE",
         notes="Auto | measured on the full window | LBA-1 co-attributed: once on P1.")


def test_undoing_the_zero_dollar_partner_takes_the_pair_out(db):
    _seed_partner_undone(db)
    led = _ledger(db).set_index("ITEM_ID")
    # the RN=1 primary inherits its partner's revert: 'partial' (its own lever still stands), with the
    # undoing change and the setting it touched carried for the flag
    p1 = led.loc["lP1"]
    assert (p1["REVERT_KIND"], p1["REVERTED_AT"], p1["REVERT_CHANGE_ID"], p1["REVERT_SETTING"],
            p1["REVERT_NEW_VALUE"]) == ("partial", "2026-07-10 06:40:00", "P3", "SIZE", "Large")
    assert (led.loc["lP2", "REVERT_KIND"], led.loc["lP2", "REVERT_SETTING"]) == ("full", "SIZE")
    assert len(led) == 2                                              # <= 1 revert row per booked change
    s = _summary(db)
    # the whole pair leaves the run-rate (the ROI numerator), and the attribution split agrees
    assert s["VERIFIED_ACTIVE_MONTHLY_USD"] == 0.0
    assert s["REVERTED_ACTIVE_ITEMS"] == 2 and s["REVERTED_ACTIVE_USD"] == pytest.approx(200.0)
    assert _run(db, mart_sql.ledger_attribution()).iloc[0]["ACTIVE_USD"] == 0.0
    # Saved to date stops at the revert: Jun 1 -> Jul 10 = 39 days x $200/30 (not 119 days to Sep 28)
    assert s["SAVED_TO_DATE_USD"] == pytest.approx(260.0)
    assert s["SAVED_BEFORE_REVERT_USD"] == pytest.approx(260.0)
    # a proven fix is a fix that stayed: WH_P is not transferable
    assert "WH_P" not in set(_run(db, mart_sql.verified_wins("ALL"))["TARGET_WAREHOUSE"])


def test_group_is_one_scan_on_one_warehouse(db, monkeypatch):
    # Q: SIZE seen Jun 1 and MAX_CLUSTERS seen Jun 5 -- two scans, two measured windows: undoing the
    #    clusters change leaves the SIZE saving alone
    _reg(db, "Q1", "WH_Q", "SIZE", "Large", "Medium", "2026-06-01 06:40:00")
    _reg(db, "Q2", "WH_Q", "MAX_CLUSTERS", "3", "2", "2026-06-05 06:40:00")
    _reg(db, "Q3", "WH_Q", "MAX_CLUSTERS", "2", "3", "2026-07-01 06:40:00")
    _led(db, "lQ1", "VERIFIED", 100.0, "2026-06-16 06:45:00", source="Q1", finding="RESIZE")
    _led(db, "lQ2", "VERIFIED", 50.0, "2026-06-20 06:45:00", source="Q2", finding="MAX_CLUSTERS")
    # R: another warehouse seen by the SAME scan is a different group
    _reg(db, "R1", "WH_R", "SIZE", "Large", "Medium", "2026-06-05 06:40:00")
    _led(db, "lR1", "VERIFIED", 70.0, "2026-06-20 06:45:00", source="R1", finding="RESIZE")
    # Z: a MIN_CLUSTERS change the same scan saw is never booked and never carries a revert
    _reg(db, "Z1", "WH_Z", "SIZE", "Large", "Medium", "2026-06-01 06:40:00")
    _reg(db, "Z2", "WH_Z", "MIN_CLUSTERS", "1", "2", "2026-06-01 06:40:00")
    _reg(db, "Z3", "WH_Z", "MIN_CLUSTERS", "2", "1", "2026-07-01 06:40:00")
    _led(db, "lZ1", "VERIFIED", 40.0, "2026-06-16 06:45:00", source="Z1", finding="RESIZE")
    led = _ledger(db).set_index("ITEM_ID")
    assert pd.isna(led.loc["lQ1", "REVERTED_AT"]) and led.loc["lQ2", "REVERT_KIND"] == "full"
    assert pd.isna(led.loc["lR1", "REVERTED_AT"]) and pd.isna(led.loc["lZ1", "REVERTED_AT"])
    s = _summary(db)
    assert s["VERIFIED_ACTIVE_MONTHLY_USD"] == pytest.approx(210.0) and s["REVERTED_ACTIVE_ITEMS"] == 1
    # the pandas fallback reads the same executed columns, so it agrees
    monkeypatch.setattr(actions_mod, "account_now", lambda: datetime(2026, 9, 28, 9, 0))
    assert ledger_totals(_ledger(db))["verified_active_usd"] == pytest.approx(210.0)


def test_an_unbooked_same_scan_increase_never_carries_a_revert(db):
    """Review r2: 'scale out, not up' -- one scan sees a booked downsize AND a cluster increase (never booked,
    not in the V153 LBA-1 partition). Raising clusters again later is capacity growth, not an undo: the
    downsize saving stays in the run-rate. A saving-direction partner still carries (the F1 case)."""
    # S: SIZE Large->Medium booked; MAX_CLUSTERS 2->3 (an increase) in the same scan; clusters 3->4 later
    _reg(db, "S1", "WH_S", "SIZE", "Large", "Medium", "2026-06-01 06:40:00")
    _reg(db, "S2", "WH_S", "MAX_CLUSTERS", "2", "3", "2026-06-01 06:40:00")
    _reg(db, "S3", "WH_S", "MAX_CLUSTERS", "3", "4", "2026-08-01 06:40:00")
    _led(db, "lS1", "VERIFIED", 150.0, "2026-06-16 06:45:00", source="S1", finding="RESIZE")
    # U: AUTO_SUSPEND 600->60 booked; SIZE Medium->Large (an increase) in the same scan; Large->X-Large later
    _reg(db, "U1", "WH_U", "AUTO_SUSPEND", "600", "60", "2026-06-01 06:40:00")
    _reg(db, "U2", "WH_U", "SIZE", "Medium", "Large", "2026-06-01 06:40:00")
    _reg(db, "U3", "WH_U", "SIZE", "Large", "X-Large", "2026-08-01 06:40:00")
    _led(db, "lU1", "VERIFIED", 200.0, "2026-06-16 06:45:00", source="U1", finding="AUTO_SUSPEND")
    led = _ledger(db).set_index("ITEM_ID")
    assert pd.isna(led.loc["lS1", "REVERTED_AT"]) and pd.isna(led.loc["lU1", "REVERTED_AT"])
    s = _summary(db)
    assert s["VERIFIED_ACTIVE_MONTHLY_USD"] == pytest.approx(350.0) and s["REVERTED_ACTIVE_ITEMS"] == 0
    # Saved to date runs to today for both (Jun 1 -> Sep 28 = 119 days)
    assert s["SAVED_TO_DATE_USD"] == pytest.approx(round(350.0 / 30 * 119, 2))
    assert {"WH_S", "WH_U"} <= set(_run(db, mart_sql.verified_wins("ALL"))["TARGET_WAREHOUSE"])


def test_same_scan_reverts_keep_their_own_kind(db):
    # T: SIZE (RN=1) + AUTO_SUSPEND ($0) seen together, then BOTH undone by one later scan (one ALTER). Each
    # row reads its OWN full revert -- never a partner's, whichever REVERT_CHANGE_ID sorts first ("T1R" <
    # "T2R" would hand lT2 the SIZE revert on a plain id tiebreak).
    _reg(db, "T1", "WH_T", "SIZE", "Large", "Medium", "2026-06-01 06:40:00")
    _reg(db, "T2", "WH_T", "AUTO_SUSPEND", "600", "60", "2026-06-01 06:40:00")
    _reg(db, "T1R", "WH_T", "SIZE", "Medium", "Large", "2026-07-01 06:40:00")
    _reg(db, "T2R", "WH_T", "AUTO_SUSPEND", "60", "600", "2026-07-01 06:40:00")
    _led(db, "lT1", "VERIFIED", 150.0, "2026-06-16 06:45:00", source="T1", finding="RESIZE")
    _led(db, "lT2", "VERIFIED", 0.0, "2026-06-16 06:45:00", source="T2", finding="AUTO_SUSPEND")
    # V: the $0 partner is undone first (Jul 1), the primary's own setting fully later (Aug 1): it left the
    # run-rate Jul 1 (the first revert), and it reads 'full' because its own lever is fully undone now
    _reg(db, "V1", "WH_V", "SIZE", "Large", "Medium", "2026-06-01 06:40:00")
    _reg(db, "V2", "WH_V", "MAX_CLUSTERS", "3", "2", "2026-06-01 06:40:00")
    _reg(db, "V2R", "WH_V", "MAX_CLUSTERS", "2", "3", "2026-07-01 06:40:00")
    _reg(db, "V1R", "WH_V", "SIZE", "Medium", "Large", "2026-08-01 06:40:00")
    _led(db, "lV1", "VERIFIED", 120.0, "2026-06-16 06:45:00", source="V1", finding="RESIZE")
    _led(db, "lV2", "VERIFIED", 0.0, "2026-06-16 06:45:00", source="V2", finding="MAX_CLUSTERS")
    led = _ledger(db).set_index("ITEM_ID")
    got = {i: (led.loc[i, "REVERT_KIND"], led.loc[i, "REVERT_CHANGE_ID"], led.loc[i, "REVERT_SETTING"])
           for i in ("lT1", "lT2", "lV1", "lV2")}
    assert got == {"lT1": ("full", "T1R", "SIZE"), "lT2": ("full", "T2R", "AUTO_SUSPEND"),
                   "lV1": ("full", "V2R", "MAX_CLUSTERS"), "lV2": ("full", "V2R", "MAX_CLUSTERS")}
    assert led.loc["lV1", "REVERTED_AT"] == "2026-07-01 06:40:00"


def test_partial_then_full_reads_full(db):
    # Y: 600 -> 60 booked; 60 -> 300 (partial) on Jul 1, then 300 -> NULL (never suspends) on Aug 1: the
    #    warehouse is now costlier than before the booked change -> 'full'; the dates / values stay the
    #    FIRST revert's (the day the saving left the run-rate)
    _reg(db, "Y", "WH_Y", "AUTO_SUSPEND", "600", "60", "2026-06-01 06:40:00")
    _reg(db, "Y2", "WH_Y", "AUTO_SUSPEND", "60", "300", "2026-07-01 06:40:00")
    _reg(db, "Y3", "WH_Y", "AUTO_SUSPEND", "300", None, "2026-08-01 06:40:00")
    _led(db, "lY", "VERIFIED", 90.0, "2026-06-16 06:45:00", source="Y", finding="AUTO_SUSPEND")
    # YP: 600 -> 60; 60 -> 120, then 120 -> 300: every later value stays below the booked OLD -> 'partial'
    _reg(db, "YP", "WH_YP", "AUTO_SUSPEND", "600", "60", "2026-06-01 06:40:00")
    _reg(db, "YP2", "WH_YP", "AUTO_SUSPEND", "60", "120", "2026-07-01 06:40:00")
    _reg(db, "YP3", "WH_YP", "AUTO_SUSPEND", "120", "300", "2026-08-01 06:40:00")
    _led(db, "lYP", "VERIFIED", 30.0, "2026-06-16 06:45:00", source="YP", finding="AUTO_SUSPEND")
    led = _ledger(db).set_index("ITEM_ID")
    y = led.loc["lY"]
    assert (y["REVERT_KIND"], y["REVERTED_AT"], y["REVERT_CHANGE_ID"], y["REVERT_NEW_VALUE"]) == (
        "full", "2026-07-01 06:40:00", "Y2", "300")
    yp = led.loc["lYP"]
    assert (yp["REVERT_KIND"], yp["REVERTED_AT"], yp["REVERT_CHANGE_ID"]) == ("partial", "2026-07-01 06:40:00",
                                                                             "YP2")
    assert len(led) == 2


# --------------------------------------------------------------------------- Saved to date
def _seed_saved(c) -> None:
    # S1 auto, V153 full-window settle: seen Aug 1, verified Aug 16 -> 58 days to Sep 28 at $10/day = 580;
    #    measured min(AFTER_DAYS 14, 15 days change->settle) = 14 days = 140
    _reg(c, "S1", "WH_S1", "SIZE", "Large", "Medium", "2026-08-01 06:40:00")
    _led(c, "s1", "VERIFIED", 300.0, "2026-08-16 06:45:00", source="S1", finding="RESIZE")
    # S2 auto, reverted Sep 10: seen Aug 10 -> 31 days at $5/day = 155 (all "before a revert"); measured 14 = 70
    _reg(c, "S2", "WH_S2", "AUTO_SUSPEND", "600", "60", "2026-08-10 06:40:00")
    _reg(c, "S2B", "WH_S2", "AUTO_SUSPEND", "60", "600", "2026-09-10 06:40:00")
    _led(c, "s2", "VERIFIED", 150.0, "2026-08-25 06:45:00", source="S2", finding="AUTO_SUSPEND")
    # S3 manual, verified Sep 18 -> 10 days at $2/day = 20; nothing measured
    _led(c, "s3", "VERIFIED", 60.0, "2026-09-18 10:00:00", finding="SCHEDULE", target="WH_S3",
         notes="booked from Optimize")
    # S4 auto, pre-V153 short settle: seen Jul 20, settled Jul 23 on ~3 days (the registry kept refreshing
    #    AFTER_DAYS to 14) -> 70 days at $3/day = 210; measured min(14, 3) = 3 days = 9
    _reg(c, "S4", "WH_S4", "MAX_CLUSTERS", "4", "2", "2026-07-20 06:40:00")
    _led(c, "s4", "VERIFIED", 90.0, "2026-07-23 06:45:00", source="S4", finding="MAX_CLUSTERS",
         notes="Auto | measured 3 -> 1 credits/day over 3d")
    # never accrue: an ESTIMATED auto row, a REJECTED row
    _reg(c, "S5", "WH_S5", "SIZE", "Large", "Small", "2026-09-20 06:40:00")
    _led(c, "s5", "ESTIMATED", None, None, source="S5", created="2026-09-20 06:45:00")
    _led(c, "s6", "REJECTED", None, "2026-09-01 07:00:00", finding="SCHEDULE", target="WH_S6",
         estimated=30.0)


def test_saved_to_date_worked_example(db):
    _seed_saved(db)
    s = _summary(db)
    assert s["SAVED_TO_DATE_USD"] == pytest.approx(580.0 + 155.0 + 20.0 + 210.0)       # 965.00
    assert s["SAVED_MEASURED_USD"] == pytest.approx(140.0 + 70.0 + 0.0 + 9.0)          # 219.00
    assert s["SAVED_BEFORE_REVERT_USD"] == pytest.approx(155.0)
    assert s["SAVED_SINCE_DATE"] == "2026-07-20"                   # the earliest start (S4's change day)
    saved = proof.saved_to_date(pd.DataFrame([s]))
    assert saved == {"total_usd": 965.0, "measured_usd": 219.0, "carried_usd": 746.0,
                     "before_revert_usd": 155.0, "since": date(2026, 7, 20)}
    # the reverted row left the run-rate but its accrual up to the revert stays
    assert s["VERIFIED_ACTIVE_MONTHLY_USD"] == pytest.approx(300.0 + 60.0 + 90.0)
    assert s["REVERTED_ACTIVE_ITEMS"] == 1 and s["REVERTED_ACTIVE_USD"] == pytest.approx(150.0)


def test_saved_to_date_start_and_null_revert_never_null_the_row(db):
    # one auto row alone: REVERTED_AT is NULL, so LEAST(today, cap, <NULL revert>) would be NULL without the
    # COALESCE -- the row must still accrue from its change day, not from VERIFIED_AT
    _reg(db, "S1", "WH_S1", "SIZE", "Large", "Medium", "2026-08-01 06:40:00")
    _led(db, "s1", "VERIFIED", 300.0, "2026-08-16 06:45:00", source="S1", finding="RESIZE")
    s = _summary(db)
    assert s["SAVED_TO_DATE_USD"] == pytest.approx(580.0)          # from Aug 1 (58 days), not Aug 16 (43)
    assert s["SAVED_SINCE_DATE"] == "2026-08-01"
    assert s["SAVED_BEFORE_REVERT_USD"] == 0.0


def test_saved_to_date_twelve_month_stop(db):
    # verified Aug 16 2025: accrues from the Aug 1 2025 change day to Aug 16 2026 (380 days), then stops --
    # it has left the run-rate too, and it keeps what it accrued
    _reg(db, "P", "WH_P", "SIZE", "Medium", "Small", "2025-08-01 06:40:00")
    _led(db, "p", "VERIFIED", 30.0, "2025-08-16 06:45:00", source="P", finding="RESIZE")
    s = _summary(db)
    assert s["SAVED_TO_DATE_USD"] == pytest.approx(380.0)
    assert s["SAVED_MEASURED_USD"] == pytest.approx(14.0)
    assert s["VERIFIED_ACTIVE_MONTHLY_USD"] == 0.0 and s["VERIFIED_ACTIVE_ITEMS"] == 0


def test_saved_to_date_revert_before_the_settle_accrues_change_to_revert(db):
    # seen Jun 1, undone Jun 10, settled Jun 16 on the full window: 9 days at $2/day, all inside the window
    _reg(db, "Q", "WH_Q", "AUTO_SUSPEND", "600", "60", "2026-06-01 06:40:00")
    _reg(db, "Q2", "WH_Q", "AUTO_SUSPEND", "60", "600", "2026-06-10 06:40:00")
    _led(db, "q", "VERIFIED", 60.0, "2026-06-16 06:45:00", source="Q", finding="AUTO_SUSPEND")
    s = _summary(db)
    assert s["SAVED_TO_DATE_USD"] == pytest.approx(18.0)
    assert s["SAVED_MEASURED_USD"] == pytest.approx(18.0)
    assert s["SAVED_BEFORE_REVERT_USD"] == pytest.approx(18.0)
    assert s["VERIFIED_ACTIVE_MONTHLY_USD"] == 0.0 and s["REVERTED_ACTIVE_ITEMS"] == 1


def test_saved_to_date_superseded_twin_never_accrues(db):
    _reg(db, "K", "WH_K", "SIZE", "Large", "Medium", "2026-09-01 06:40:00")
    _led(db, "k", "VERIFIED", 90.0, "2026-09-16 06:45:00", source="K", finding="RESIZE")
    _led(db, "km", "VERIFIED", 150.0, "2026-09-10 09:00:00", finding="RESIZE", target="WH_K",
         created="2026-08-31 10:00:00", notes="booked from Optimize")
    s = _summary(db)
    assert s["SUPERSEDED_ITEMS"] == 1
    assert s["SAVED_TO_DATE_USD"] == pytest.approx(90.0 / 30 * 27)        # the auto row only, from Sep 1


def test_saved_to_date_null_verified_at_accrues_nothing_and_nulls_nothing(db):
    # a VERIFIED row without VERIFIED_AT (never written by the app or the proc) accrues 0 -- its 12-month
    # stop is unknown -- and never NULLs the total or the since-date of the rows beside it
    _reg(db, "S1", "WH_S1", "SIZE", "Large", "Medium", "2026-08-01 06:40:00")
    _led(db, "s1", "VERIFIED", 300.0, "2026-08-16 06:45:00", source="S1", finding="RESIZE")
    _reg(db, "N", "WH_N", "SIZE", "Large", "Medium", "2026-09-01 06:40:00")
    _led(db, "n", "VERIFIED", 90.0, None, source="N", finding="RESIZE", created="2026-09-01 06:45:00")
    _led(db, "m", "VERIFIED", 30.0, None, finding="SCHEDULE", target="WH_M", created="2026-09-01 06:45:00",
         notes="booked from Optimize")
    s = _summary(db)
    assert s["SAVED_TO_DATE_USD"] == pytest.approx(580.0) and s["SAVED_MEASURED_USD"] == pytest.approx(140.0)
    assert s["SAVED_SINCE_DATE"] == "2026-08-01"


def test_empty_ledger_gives_null_sums_without_raising(db):
    s = _summary(db)
    assert pd.isna(s["SAVED_TO_DATE_USD"]) and pd.isna(s["SAVED_SINCE_DATE"])
    # Snowflake's COUNT_IF over zero rows is 0; a sqlite user aggregate that never stepped returns NULL.
    # Proof reads both through int(safe_float(...)), so either is 0 on the page.
    assert int(s["VERIFIED_ACTIVE_ITEMS"] or 0) == 0 and int(s["REVERTED_ACTIVE_ITEMS"] or 0) == 0
    saved = proof.saved_to_date(pd.DataFrame([s]))
    assert saved == {"total_usd": 0.0, "measured_usd": 0.0, "carried_usd": 0.0, "before_revert_usd": 0.0,
                     "since": None}
