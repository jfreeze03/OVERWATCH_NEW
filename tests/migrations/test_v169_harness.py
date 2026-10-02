"""Executed harness for V169's SP_ALERT_SCAN_DAILY deltas -- [08] COST_BUDGET_PACE / [09] COST_FORECAST_BREACH
(R2-041), [16] COST_CONTRACT_BREACH (R2-042 + R2-103), [12] COST_STORAGE_SURGE (R2-044), [18] DQ_RECON_ERROR
(R2-020 = R2-043), [19] COST_EGRESS_SPIKE (R2-047), [24] COST_IDLE_OPPORTUNITY (R1-071) and [29]
SEC_TRUST_REGRESSION (R1-233).

Each arm's OWN INSERT, read from the V169 migration, runs in an in-memory sqlite; where a test needs teeth the same
fixture runs V163's text (the defect, reproduced). A DATE is 'YYYY-MM-DD'; a timestamp is integer milliseconds of
Central wall-clock time, so CONVERT_TIMEZONE('America/Chicago', x)::DATE is the Central day. Comments are dropped
before translation; any '::' cast, QUALIFY, ILIKE, CONVERT_TIMEZONE or :bind left over raises. ALERT_EVENTS carries
its V004 widths as CHECKs. [24] runs on tests/migrations/test_v157's idle fixture (the app parity) and [29] on
tests/migrations/test_v163_harness._Db (both shared, never copied).
"""

from __future__ import annotations

import itertools
import math
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest

from tests._source import ROOT, read
from tests.migrations import test_v157_alert_scan_self_watch_idle_push as v157
from tests.migrations.test_v163_harness import _Db as _TrustDb
from tests.migrations.test_v163_harness import _lex

_V169 = read("snowflake/migrations/V169__alert_scan_daily_windows_and_keys.sql")
_V163 = read("snowflake/migrations/V163__ai_runaway_trust_regression.sql")


def _proc(text: str, name: str) -> str:
    s = text.index(f"CREATE OR REPLACE PROCEDURE DBA_MAINT_DB.OVERWATCH.{name}")
    o = text.index("$$", s)
    return text[s:text.index("$$;", o + 2) + 3]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


_D, _D163 = _proc(_V169, "SP_ALERT_SCAN_DAILY()"), _proc(_V163, "SP_ALERT_SCAN_DAILY()")
_SPANS = {
    "08": ("    -- [08] COST_BUDGET_PACE\n", "    -- [09] COST_FORECAST_BREACH\n"),
    "09": ("    -- [09] COST_FORECAST_BREACH\n", "    -- [13b] COST_AI_CREEP\n"),
    "16": ("    -- [16] COST_CONTRACT_BREACH\n", "    -- [12] COST_STORAGE_SURGE\n"),
    "12": ("    -- [12] COST_STORAGE_SURGE\n", "    -- [13] COST_SERVERLESS_CREEP\n"),
    "19": ("    -- [19] COST_EGRESS_SPIKE", "    -- [22] OPS_PIPELINE_DEGRADED"),
    "18": ("    -- [18] DQ_RECON_ERROR", "    IF (fails > 0) THEN"),
    "24": ("    -- [24] COST_IDLE_OPPORTUNITY", "    -- [25] COST_SLEEP_POLLING"),
    "29": ("    -- [29] SEC_TRUST_REGRESSION", "    -- [17] PIPE_REF_GAP"),
}


def _stmt(body: str, arm: str) -> str:
    block = _between(body, *_SPANS[arm])
    return block[block.index("        WITH cfg AS ("):block.index(";\n    EXCEPTION")]


def _insert(body: str, arm: str) -> str:
    block = _between(body, *_SPANS[arm])
    return block[block.index("        INSERT INTO DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS"):block.index(";\n    EXCEPTION")]


# ============================================================================================================
# Snowflake -> sqlite
# ============================================================================================================
_EPOCH = datetime(1970, 1, 1)
_UNIT_MS = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000}


def _ms(d: datetime) -> int:
    return round((d - _EPOCH).total_seconds() * 1000)


def _iso(d: date) -> str:
    return d.isoformat()


def _dateadd(unit, n, x):
    if unit is None or n is None or x is None:
        return None
    u = str(unit).lower()
    if isinstance(x, str):
        assert u == "day", u
        return (date.fromisoformat(x[:10]) + timedelta(days=int(n))).isoformat()
    return int(x) + int(n) * _UNIT_MS[u]


def _to_date(x):
    if x is None:
        return None
    if isinstance(x, str):
        return x[:10]
    return (_EPOCH + timedelta(milliseconds=int(x))).date().isoformat()


def _try_to_date(x):
    try:
        return date.fromisoformat(str(x)).isoformat() if x is not None else None
    except ValueError:
        return None


def _try_to_double(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _date_trunc(part, x):
    if x is None:
        return None
    d = date.fromisoformat(str(x)[:10])
    p = str(part).lower()
    if p == "month":
        return d.replace(day=1).isoformat()
    assert p == "week", p
    return (d - timedelta(days=d.weekday())).isoformat()


def _last_day(x):
    d = date.fromisoformat(str(x)[:10])
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (nxt - timedelta(days=1)).isoformat()


def _nullsafe(fn):
    return lambda *a: None if any(v is None for v in a) else fn(*a)


class _MaxBy:
    def __init__(self) -> None:
        self.best = None

    def step(self, value, key) -> None:
        if key is not None and (self.best is None or key > self.best[0]):
            self.best = (key, value)

    def finalize(self):
        return None if self.best is None else self.best[1]


class _ListAggByDesc:
    """LISTAGG(x, ', ') WITHIN GROUP (ORDER BY n DESC)."""

    def __init__(self) -> None:
        self.v: list[tuple] = []

    def step(self, x, n) -> None:
        if x is not None:
            self.v.append((n, str(x)))

    def finalize(self):
        return ", ".join(x for _n, x in sorted(self.v, key=lambda t: -t[0])) if self.v else None


_SCHEMAS = {
    "ALERT_CONFIG": "RULE_ID, SEVERITY, ENABLED, THRESHOLD_NUM, WINDOW_HOURS",
    "SETTINGS": "KEY, VALUE",
    "ALERT_EVENTS": ("EVENT_ID INTEGER PRIMARY KEY AUTOINCREMENT, RULE_ID, COMPANY, SEVERITY, "
                     "TITLE NOT NULL CHECK (length(TITLE) <= 300), DETAIL CHECK (length(DETAIL) <= 2000), "
                     "METRIC_VALUE, DEDUPE_KEY CHECK (length(DEDUPE_KEY) <= 300), STATUS DEFAULT 'OPEN'"),
    "FACT_METERING_DAILY": "DAY, SERVICE_TYPE, CREDITS_BILLED",
    "DATABASE_STORAGE_USAGE_HISTORY": "DATABASE_ID, DATABASE_NAME, USAGE_DATE, AVERAGE_DATABASE_BYTES, DELETED",
    "ETL_RECON_RESULTS": "MTRC, N, LATEST_LOAD",
    "DATA_TRANSFER_HISTORY": "START_TIME, TARGET_REGION, TARGET_CLOUD, BYTES_TRANSFERRED",
}
_BINDS = {":budget_usd": "{budget}", ":ai_credit_price": "2.2", ":credit_price": "3.68"}


def _qualify(sql: str) -> str:
    """``(\\n SELECT <cols> ... QUALIFY <rn> = 1\\n )`` -> a ROW_NUMBER subquery (sqlite has no QUALIFY)."""
    while m := re.search(r"\n[ ]*QUALIFY (?P<rn>[^\n]+) = 1\n", sql):
        start = sql.rindex("(\n", 0, m.start()) + 2
        first, rest = sql[start:m.start()].split("\n", 1) if "\n" in sql[start:m.start()] else (sql[start:m.start()], "")
        while not first.strip():
            first, rest = rest.split("\n", 1)
        assert first.lstrip().startswith("SELECT "), first
        first = first.replace("SELECT ", f"SELECT {m['rn']} AS QRN_, ", 1)
        sql = sql[:start] + "SELECT * FROM (" + first + "\n" + rest + "\n) WHERE QRN_ = 1\n" + sql[m.end():]
    return sql


def _to_sqlite(sql: str, budget: float) -> str:
    s = "".join(t for kind, t in _lex(sql) if kind != "comment")
    s = s.replace("DBA_MAINT_DB.OVERWATCH.", "").replace("SNOWFLAKE.ACCOUNT_USAGE.", "")
    s = s.replace("CONVERT_TIMEZONE('America/Chicago', CURRENT_TIMESTAMP())::DATE", "TODAY_D()")
    s = re.sub(r"CONVERT_TIMEZONE\('America/Chicago', ([\w.]+)\)::DATE", r"TO_DATE(\1)", s)
    s = s.replace("'9999-12-31'::DATE", "'9999-12-31'")
    s = s.replace("CURRENT_DATE()", "TODAY_D()").replace("CURRENT_TIMESTAMP()", "NOW_TS()")
    s = re.sub(r"\bILIKE\b", "LIKE", s)
    s = re.sub(r"LISTAGG\((\w+), ', '\) WITHIN GROUP \(ORDER BY (\w+) DESC\)", r"LISTAGG_BYDESC(\1, \2)", s)
    for k, v in _BINDS.items():
        s = s.replace(k, v.format(budget=repr(float(budget))))
    s = _qualify(s)
    s = v157._derived_to_cte(s)
    code = "".join(t for kind, t in _lex(s) if kind == "code")
    for bad in ("::", "QUALIFY", "ILIKE", "CONVERT_TIMEZONE", "WITHIN GROUP"):
        assert bad not in code, (bad, code)
    assert not re.search(r"(?<![:\w]):\w", code), code
    return s


class _Daily:
    def __init__(self, today: date, *, cfg: dict | None = None, settings: dict | None = None,
                 budget: float = 0.0) -> None:
        self.today, self.budget = today, budget
        self.cfg = {"COST_BUDGET_PACE": ("HIGH", 1.10), "COST_FORECAST_BREACH": ("CRITICAL", 1.00),
                    "COST_CONTRACT_BREACH": ("HIGH", 30), "COST_STORAGE_SURGE": ("MEDIUM", 100),
                    "DQ_RECON_ERROR": ("HIGH", 1), "COST_EGRESS_SPIKE": ("MEDIUM", 100), **(cfg or {})}
        self.settings = settings or {}
        self.tables: dict[str, list[tuple]] = {k: [] for k in _SCHEMAS if k not in ("ALERT_CONFIG", "SETTINGS",
                                                                                     "ALERT_EVENTS")}
        self.events: list[tuple] = []

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(":memory:")
        now = _ms(datetime(self.today.year, self.today.month, self.today.day, 7, 5))      # the ~07:00 scan
        funcs = {
            "TODAY_D": (0, lambda: self.today.isoformat()), "NOW_TS": (0, lambda: now),
            "DATEADD": (3, _dateadd), "TO_DATE": (1, _to_date), "TRY_TO_DATE": (1, _try_to_date),
            "TRY_TO_DOUBLE": (1, _try_to_double), "DATE_TRUNC": (2, _date_trunc),
            "DAY": (1, _nullsafe(lambda x: date.fromisoformat(str(x)[:10]).day)), "LAST_DAY": (1, _last_day),
            "IFF": (3, lambda c, a, b: a if c else b), "CEIL": (1, _nullsafe(lambda x: math.ceil(x))),
            "POWER": (2, _nullsafe(lambda a, b: float(a) ** float(b))),
            "LEFT": (2, _nullsafe(lambda s, n: str(s)[:int(n)])),
            "TO_VARCHAR": (1, lambda x: None if x is None else str(x)),
            "COMPANY_FOR_DATABASE": (1, lambda _d: "ALFA"),
        }
        for name, (narg, fn) in funcs.items():
            con.create_function(name, narg, fn)
        con.create_aggregate("MAX_BY", 2, _MaxBy)
        con.create_aggregate("LISTAGG_BYDESC", 2, _ListAggByDesc)
        for table, cols in _SCHEMAS.items():
            con.execute(f"CREATE TABLE {table} ({cols})")
        for rid, (sev, thr) in self.cfg.items():
            con.execute("INSERT INTO ALERT_CONFIG VALUES (?, ?, 1, ?, 48)", (rid, sev, thr))
        con.executemany("INSERT INTO SETTINGS VALUES (?, ?)", list(self.settings.items()))
        for table, rows in self.tables.items():
            for row in rows:
                con.execute(f"INSERT INTO {table} VALUES ({', '.join('?' * len(row))})", row)
        for ev in self.events:
            con.execute("INSERT INTO ALERT_EVENTS (RULE_ID, COMPANY, SEVERITY, TITLE, DETAIL, METRIC_VALUE, DEDUPE_KEY)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)", ev)
        return con

    def run(self, insert: str) -> list[dict]:
        """Execute an arm's INSERT (V004 widths enforced); -> the rows it inserted; they stay as events."""
        con = self._con()
        con.execute(_to_sqlite(insert, self.budget))
        cols = ("RULE_ID", "COMPANY", "SEVERITY", "TITLE", "DETAIL", "METRIC_VALUE", "DEDUPE_KEY")
        rows = [dict(zip(cols, r, strict=True)) for r in
                con.execute(f"SELECT {', '.join(cols)} FROM ALERT_EVENTS ORDER BY EVENT_ID").fetchall()]
        new = rows[len(self.events):]
        self.events += [tuple(r.values()) for r in new]
        return new


def _arm(arm: str, old: bool = False) -> str:
    return _insert(_D163 if old else _D, arm)


def _n(text: str) -> str:
    """sqlite renders ROUND(x, 0) as '1500.0' where Snowflake renders '1500': compare the integral form."""
    return re.sub(r"(?<![\d.])(\d+)\.0(?![\d])", r"\1", text)


# ============================================================================================================
# [08] / [09] (R2-041): complete-day MTD
# ============================================================================================================
_B, _DIM = 30000.0, 30                      # September: 30 days -> B/DIM = 1000 USD/day


def _metering(db: _Daily, dom: int, per_day_usd: float, today_share: float = 0.5) -> _Daily:
    """(dom - 1) complete days at per_day_usd, split across a compute and an AI row (two-partition pricing), plus
    today's partial row at today_share of a day."""
    ai_cr = 100.0                                                     # 220 USD at 2.20
    for k in range(1, dom):
        d = date(2026, 9, k).isoformat()
        db.tables["FACT_METERING_DAILY"] += [(d, "WAREHOUSE_METERING", (per_day_usd - ai_cr * 2.2) / 3.68),
                                             (d, "AI_SERVICES", ai_cr)]
    if today_share:
        db.tables["FACT_METERING_DAILY"].append(
            (date(2026, 9, dom).isoformat(), "WAREHOUSE_METERING", per_day_usd * today_share / 3.68))
    return db


@pytest.mark.parametrize("dom", [2, 3, 4, 5, 15])
def test_budget_pace_no_longer_false_fires_early_in_an_on_budget_month(dom):
    db = _metering(_Daily(date(2026, 9, dom), budget=_B), dom, _B / _DIM)
    assert db.run(_arm("08")) == []
    if dom == 2:      # teeth: V163 compared the today-inclusive MTD with the completed-days allowance
        (old,) = _metering(_Daily(date(2026, 9, dom), budget=_B), dom, _B / _DIM).run(_arm("08", old=True))
        assert _n(old["TITLE"]).startswith("MTD spend $1500 is 1.5x")


def test_budget_pace_fires_on_a_real_overrun_with_the_complete_day_numbers():
    db = _metering(_Daily(date(2026, 9, 3), budget=_B), 3, 1.2 * _B / _DIM)
    (ev,) = db.run(_arm("08"))
    assert _n(ev["TITLE"]) == "MTD spend through yesterday $2400 is 1.2x the budget pace"
    assert ev["METRIC_VALUE"] == pytest.approx(2400.0)
    assert _n(ev["DETAIL"]) == ("Budget $30000/mo; elapsed-share allowance $2000 for 2 complete day(s); the partial "
                                "metering of today is not counted.")
    assert ev["DEDUPE_KEY"] == "COST_BUDGET_PACE|ALL|2026-09-03"
    assert db.run(_arm("08")) == []


def test_forecast_projects_today_and_catches_what_v163_missed():
    on = _metering(_Daily(date(2026, 9, 15), budget=_B), 15, _B / _DIM)
    assert on.run(_arm("09")) == []
    con = on._con()
    proj = con.execute(_to_sqlite(_arm("09").replace("AND (m.MTD_COMPLETE_USD", "AND (0 * m.MTD_COMPLETE_USD")
                                  .replace("> :budget_usd * c.THRESHOLD_NUM", "> -1"), _B)).rowcount
    assert proj == 1
    (row,) = con.execute("SELECT METRIC_VALUE, TITLE FROM ALERT_EVENTS").fetchall()
    assert row[0] == pytest.approx(_B) and "exceeds budget $30000" in _n(row[1])
    over = _metering(_Daily(date(2026, 9, 15), budget=_B), 15, 1.01 * _B / _DIM)
    (ev,) = over.run(_arm("09"))
    assert ev["METRIC_VALUE"] == pytest.approx(1.01 * _B)
    assert _n(ev["TITLE"]) == "Projected month-end $30300 exceeds budget $30000"
    assert _n(ev["DETAIL"]) == "MTD through yesterday $14140 + $1010/day x 16 remaining days incl. today."
    # teeth: V163 projected MTD (half of today) + rate x 15 days: 29795 < 30000 -> missed
    assert _metering(_Daily(date(2026, 9, 15), budget=_B), 15, 1.01 * _B / _DIM).run(_arm("09", old=True)) == []


def test_day_one_raises_neither_arm():
    db = _metering(_Daily(date(2026, 9, 1), budget=_B), 1, 5 * _B / _DIM)
    assert db.run(_arm("08")) == [] and db.run(_arm("09")) == []
    assert _metering(_Daily(date(2026, 9, 5), budget=0.0), 5, 9 * _B / _DIM).run(_arm("08")) == []   # no budget


# ============================================================================================================
# [16] COST_CONTRACT_BREACH (R2-042 + R2-103)
# ============================================================================================================
def _contract(today: date, consumed: float, *, burn: float = 80.0, start: str = "2026-01-01",
              end: str = "2027-01-01", credits: str = "100000", post_term: float = 0.0) -> _Daily:
    """CONTRACT_END_DATE is EXCLUSIVE (the term's last day is end - 1). ``consumed`` = credits in the term through
    yesterday, the last 30 complete days at ``burn``."""
    db = _Daily(today, settings={"CONTRACT_CREDITS": credits, "CONTRACT_START_DATE": start, "CONTRACT_END_DATE": end})
    rows = db.tables["FACT_METERING_DAILY"]
    tail = [(today - timedelta(days=k)).isoformat() for k in range(1, 31)]
    rows += [(d, "WAREHOUSE_METERING", burn) for d in tail]
    rows.append(("2026-01-02", "WAREHOUSE_METERING", consumed - burn * 30 - post_term))
    return db


def test_contract_outlasting_its_term_does_not_page():
    """(A) 12-20, 99,000 of 100,000 consumed, burn 80, term through 12-31: exhausts 2027-01-02, after the term."""
    db = _contract(date(2026, 12, 20), 99000)
    assert db.run(_arm("16")) == []
    (old,) = _contract(date(2026, 12, 20), 99000).run(_arm("16", old=True))      # V163 paged CRITICAL
    assert old["SEVERITY"] == "CRITICAL" and old["METRIC_VALUE"] == 13


def test_contract_exhausting_inside_its_term_still_pages_and_the_exh_band_survives():
    (crit,) = _contract(date(2026, 12, 20), 99500).run(_arm("16"))              # (B) 7 days -> 12-27
    assert crit["TITLE"] == "Contract projected to exhaust in 7 day(s) (2026-12-27)" and crit["SEVERITY"] == "CRITICAL"
    assert crit["DEDUPE_KEY"] == "COST_CONTRACT_BREACH|CRIT|2026-12-14"
    (exh,) = _contract(date(2026, 12, 20), 101000).run(_arm("16"))              # (E) in-term exhausted
    assert _n(exh["TITLE"]).startswith("Contract EXHAUSTED: 1000 credits over") and "|EXH|" in exh["DEDUPE_KEY"]


def test_contract_after_its_term_is_quiet():
    """(C) 2027-01-05, the settings not rolled forward: nothing (credits after the end never count either)."""
    db = _contract(date(2027, 1, 5), 99000, post_term=80 * 4)
    assert db.run(_arm("16")) == []
    assert len(_contract(date(2027, 1, 5), 99000, post_term=80 * 4).run(_arm("16", old=True))) == 1


def test_contract_blank_end_is_v163_and_blank_start_is_not_configured():
    for consumed in (99000, 99500, 101000):                                    # (D) blank end == V163
        new = _contract(date(2026, 12, 20), consumed, end="").run(_arm("16"))
        old = _contract(date(2026, 12, 20), consumed, end="").run(_arm("16", old=True))
        assert new == old and len(new) == 1, consumed
    # (F) R2-103: credits set, start blank -> TOTAL 0 -> nothing; V163 fabricated a runway from ~0 consumed
    db = _contract(date(2026, 12, 20), 0, start="", credits="20000", burn=800.0)
    db.tables["FACT_METERING_DAILY"] = [r for r in db.tables["FACT_METERING_DAILY"] if r[0] != "2026-01-02"]
    assert db.run(_arm("16")) == []
    db_old = _contract(date(2026, 12, 20), 0, start="", credits="20000", burn=800.0)
    db_old.tables["FACT_METERING_DAILY"] = [r for r in db_old.tables["FACT_METERING_DAILY"] if r[0] != "2026-01-02"]
    (fab,) = db_old.run(_arm("16", old=True))
    assert _n(fab["DETAIL"]).startswith("Consumed 0 of 20000")


def test_contract_exhausting_on_the_last_term_day_pages():
    """(G) the boundary: (TOTAL - CONSUMED) / burn = end - today - 1 -> exhaustion on the term's last day raises."""
    today = date(2026, 12, 20)
    left_days = (date(2027, 1, 1) - today).days - 1                             # 11 -> EXHAUST 12-31 < 01-01
    (ev,) = _contract(today, 100000 - 80 * left_days).run(_arm("16"))
    assert ev["TITLE"].endswith("(2026-12-31)")
    assert _contract(today, 100000 - 80 * (left_days + 1)).run(_arm("16")) == []   # 01-01 = the end: quiet


# ============================================================================================================
# [12] COST_STORAGE_SURGE (R2-044)
# ============================================================================================================
_GB = 1024 ** 3
_T12 = date(2026, 9, 30)


def _storage(rows: list[tuple]) -> _Daily:
    db = _Daily(_T12)
    db.tables["DATABASE_STORAGE_USAGE_HISTORY"] = list(rows)
    return db


def _recreated(dropped_gb: tuple[float, float] = (500, 5)) -> list[tuple]:
    """DEV_X re-created: live id 2 (518 then 520 GB) beside the dropped id 1 still billing Time Travel."""
    d1, d2 = (_T12 - timedelta(days=2)).isoformat(), (_T12 - timedelta(days=1)).isoformat()
    dropped = _ms(datetime(2026, 9, 27))
    return [(2, "DEV_X", d1, 518 * _GB, None), (2, "DEV_X", d2, 520 * _GB, None),
            (1, "DEV_X", d1, dropped_gb[0] * _GB, dropped), (1, "DEV_X", d2, dropped_gb[1] * _GB, dropped)]


def test_storage_surge_never_pairs_a_dropped_id_with_the_live_one():
    for rows in (_recreated(), _recreated((5, 5))):
        for perm in itertools.permutations(rows):
            assert _storage(perm).run(_arm("12")) == [], perm
    # teeth: by name, a row order pairs the live 520 GB row with the near-empty dropped row (a false +515 GB)
    old = [len(_storage(perm).run(_arm("12", old=True))) for perm in itertools.permutations(_recreated((5, 5)))]
    assert any(old) and not all(old)                                       # order-dependent, as R2-044 says


def test_storage_surge_still_raises_for_a_real_live_growth():
    d1, d2 = (_T12 - timedelta(days=2)).isoformat(), (_T12 - timedelta(days=1)).isoformat()
    db = _storage([*_recreated(), (3, "PROD_Y", d1, 100 * _GB, None), (3, "PROD_Y", d2, 500 * _GB, None)])
    (ev,) = db.run(_arm("12"))
    assert ev["DEDUPE_KEY"] == f"COST_STORAGE_SURGE|PROD_Y|{d2}" and ev["METRIC_VALUE"] == pytest.approx(400.0)
    assert ev["TITLE"] == "PROD_Y grew 400.0 GB in a day"


def test_storage_surge_ignores_a_new_database_and_a_dropped_only_one():
    d2 = (_T12 - timedelta(days=1)).isoformat()
    d1 = (_T12 - timedelta(days=2)).isoformat()
    gone = _ms(datetime(2026, 9, 28))
    db = _storage([(4, "NEW_Z", d2, 900 * _GB, None),                          # (c) one live day: no PREV
                   (5, "OLD_W", d1, 10 * _GB, gone), (5, "OLD_W", d2, 600 * _GB, gone)])   # (d) dropped only
    assert db.run(_arm("12")) == []


# ============================================================================================================
# [18] DQ_RECON_ERROR (R2-020 = R2-043)
# ============================================================================================================
def _recon(today: date, loads: list[tuple[str, int, datetime]], thr: float = 1) -> _Daily:
    db = _Daily(today, cfg={"DQ_RECON_ERROR": ("HIGH", thr)})
    db.tables["ETL_RECON_RESULTS"] = [(m, n, _ms(at)) for m, n, at in loads]
    return db


def test_recon_break_pages_once_per_error_cycle():
    mon, tue = date(2026, 9, 28), date(2026, 9, 29)
    loads = [("POLICY.PREMIUM.AMT", 12, datetime(2026, 9, 28, 4)), ("CLM.PAID.X", 3, datetime(2026, 9, 28, 3))]
    db = _recon(mon, loads)
    (ev,) = db.run(_arm("18"))
    assert ev["DEDUPE_KEY"] == "DQ_RECON_ERROR|2026-09-28" and ev["METRIC_VALUE"] == 15
    assert ev["TITLE"] == "15 reconciliation error(s) across 2 metric(s)"
    assert "Metric(s): POLICY.PREMIUM.AMT, CLM.PAID.X" in ev["DETAIL"]
    db.today = tue                                                             # (a) same rows still in the 48h window
    assert db.run(_arm("18")) == []
    db.tables["ETL_RECON_RESULTS"].append(("CLM.PAID.X", 2, _ms(datetime(2026, 9, 29, 3))))   # a new failing cycle
    assert [e["DEDUPE_KEY"] for e in db.run(_arm("18"))] == ["DQ_RECON_ERROR|2026-09-29"]
    assert db.run(_arm("18")) == []
    # teeth: V163 keyed the scan date, so the same rows paged again the next morning
    old = _recon(mon, loads)
    old.run(_arm("18", old=True))
    old.today = tue
    assert [e["DEDUPE_KEY"] for e in old.run(_arm("18", old=True))] == ["DQ_RECON_ERROR|2026-09-29"]


def test_recon_transition_and_degenerate_threshold():
    tue = date(2026, 9, 29)
    db = _recon(tue, [("M", 1, datetime(2026, 9, 29, 4))])
    db.events.append(("DQ_RECON_ERROR", "ALL", "HIGH", "x", None, 1, "DQ_RECON_ERROR|2026-09-29"))   # (c) a V163 key
    assert db.run(_arm("18")) == []
    empty = _recon(tue, [], thr=0)                                             # (d) THRESHOLD 0, no rows: never NULL
    (ev,) = empty.run(_arm("18"))
    assert ev["DEDUPE_KEY"] == "DQ_RECON_ERROR|2026-09-29"
    # (e) a widened window (days-old rows) does not re-page on consecutive mornings
    old = _recon(date(2026, 9, 20), [("M", 4, datetime(2026, 9, 2, 4))])
    assert len(old.run(_arm("18"))) == 1
    for k in range(1, 6):
        old.today = date(2026, 9, 20 + k)
        assert old.run(_arm("18")) == []
    # the second key segment still parses as a DATE (V072 ACCOUNT entity, V117 carry-forward)
    assert date.fromisoformat(ev["DEDUPE_KEY"].split("|")[1])


def _flat_header() -> str:
    head = _V169[:_V169.index("EXECUTE IMMEDIATE")]
    return " ".join(" ".join(ln[2:].strip() for ln in head.splitlines() if ln.startswith("--")).split())


def test_recon_residuals_are_codified_and_named_in_the_first_run_header():
    """Review r1: the cycle-day key keeps two residuals, both documented in V169's FIRST RUN paragraph.
    (a) once, at the transition: the pre-V169 scan on A (~07:00) keyed the SCAN date |A; a NEW failing cycle loads at
    A 21:00, so the V169 scan on A+1 keys it |A too and it folds into the old event -- V163 would have paged |A+1.
    (b) standing: a failing re-run that loads on A after A's own page folds into |A and does not page on A+1."""
    a, a1 = date(2026, 9, 29), date(2026, 9, 30)
    loads = [("M", 2, datetime(2026, 9, 28, 23)), ("M2", 1, datetime(2026, 9, 29, 21))]
    db = _recon(a1, loads)
    db.events.append(("DQ_RECON_ERROR", "ALL", "HIGH", "x", None, 2, "DQ_RECON_ERROR|2026-09-29"))  # V163 scan on A
    assert db.run(_arm("18")) == []                                                           # (a) folded
    old = _recon(a1, loads)
    old.events.append(("DQ_RECON_ERROR", "ALL", "HIGH", "x", None, 2, "DQ_RECON_ERROR|2026-09-29"))
    assert [e["DEDUPE_KEY"] for e in old.run(_arm("18", old=True))] == ["DQ_RECON_ERROR|2026-09-30"]
    db = _recon(a, [("M", 2, datetime(2026, 9, 29, 4))])
    assert [e["DEDUPE_KEY"] for e in db.run(_arm("18"))] == ["DQ_RECON_ERROR|2026-09-29"]
    db.tables["ETL_RECON_RESULTS"].append(("M", 1, _ms(datetime(2026, 9, 29, 15))))          # the same-date re-run
    db.today = a1
    assert db.run(_arm("18")) == []                                                           # (b) folded
    flat = _flat_header()
    for phrase in ("DQ_RECON_ERROR keeps two residuals of its cycle-day key",
                   "(a) Once, at the transition:", "folds into that older event and is not paged",
                   "(b) Standing: a failing re-run", "does not page the next morning",
                   "PREFLIGHT P169.4 shows the newest load and its hour", "PART B V169.4"):
        assert phrase in flat, phrase


def _part_b_grid(tmp_path, label: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("V169_OUT", "PREFLIGHT_OUT", "PART_B_OUT", "REPAIR_OUT")}
    env.update(V169_OUT=str(tmp_path / "m.sql"), PART_B_OUT=str(tmp_path / "pb.sql"))
    out = subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v169.py")], env=env, cwd=tmp_path,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    pb = (tmp_path / "pb.sql").read_text(encoding="utf-8")
    grid = pb[pb.index(f"-- {label} "):]
    return grid[:grid.index(";\n") + 1]


def _grid_rows(sql: str, recon: list[tuple[str, str]], events: list[tuple[str, str]]) -> list[dict]:
    """Run a read-only grid on sqlite: ETL_RECON_RESULTS (MTRC, LATEST_LOAD) and ALERT_EVENTS (DEDUPE_KEY, RAISED_AT),
    timestamps as ISO text (both TIMESTAMP_NTZ in Snowflake, compared as stored)."""
    code = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    code = code.replace("DBA_MAINT_DB.OVERWATCH.", "").rstrip().rstrip(";")
    con = sqlite3.connect(":memory:")
    con.create_function("TO_DATE", 1, lambda x: None if x is None else str(x)[:10])
    con.create_function("TO_VARCHAR", 1, lambda x: None if x is None else str(x))
    con.execute("CREATE TABLE ETL_RECON_RESULTS (MTRC, N, LATEST_LOAD)")
    con.execute("CREATE TABLE ALERT_EVENTS (EVENT_ID INTEGER PRIMARY KEY AUTOINCREMENT, DEDUPE_KEY, RAISED_AT)")
    con.executemany("INSERT INTO ETL_RECON_RESULTS VALUES (?, 1, ?)", recon)
    con.executemany("INSERT INTO ALERT_EVENTS (DEDUPE_KEY, RAISED_AT) VALUES (?, ?)", events)
    cur = con.execute(code)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def test_part_b_v169_4_flags_a_newest_cycle_covered_only_by_an_older_event(tmp_path):
    """Review r1: PART B V169.4 (after the first post-apply daily scan) reads OK when the newest error cycle has an
    event raised after it loaded, CHECK when it folded into an older one (the transition / same-date residual) or
    has no event at all, and OK with nothing in the look-back."""
    sql = _part_b_grid(tmp_path, "V169.4")
    key = "DQ_RECON_ERROR|2026-09-29"
    paged = _grid_rows(sql, [("M", "2026-09-29 04:00:00")], [(key, "2026-09-29 07:05:00")])
    assert [r["RESULT"] for r in paged] == ["OK"]
    folded = _grid_rows(sql, [("M", "2026-09-28 23:00:00"), ("M2", "2026-09-29 21:00:00")],
                        [(key, "2026-09-29 07:05:00")])
    (r,) = folded
    assert r["RESULT"].startswith("CHECK: the cycle loaded 2026-09-29 21:00:00 folded into ")
    assert "2026-09-29 07:05:00" in r["RESULT"] and "Reconciliation errors" in r["RESULT"]
    (none,) = _grid_rows(sql, [("M", "2026-09-29 04:00:00")], [])
    assert none["RESULT"].startswith("CHECK: no event keyed DQ_RECON_ERROR|2026-09-29")
    assert [r["RESULT"] for r in _grid_rows(sql, [], [])] == ["OK: no reconciliation error in the look-back"]
    assert {r["CHECK_NAME"] for r in paged + folded} == {"V169.4 newest reconciliation error cycle was paged"}


# ============================================================================================================
# [19] COST_EGRESS_SPIKE (R2-047)
# ============================================================================================================
_T19 = date(2026, 9, 30)
_Y19 = _T19 - timedelta(days=1)


def _xfer(rows: list[tuple]) -> _Daily:
    """rows: (Central datetime, region, cloud, GB)."""
    db = _Daily(_T19)
    db.tables["DATA_TRANSFER_HISTORY"] = [(_ms(at), reg, cloud, gb * _GB) for at, reg, cloud, gb in rows]
    return db


def _at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm)


def test_egress_names_the_days_largest_per_region_total_not_the_largest_row():
    old_row = (_at(_T19 - timedelta(days=10), 12), "AWS_US_WEST_2", "AWS", 160)
    today_rows = [(_at(_Y19, h), "AZURE_EASTUS2", "AZURE", 50) for h in (9, 13, 17)]
    (ev,) = _xfer([old_row, *today_rows]).run(_arm("19"))
    assert ev["TITLE"] == f"Egress 150.0 GB on {_Y19.isoformat()} (14d avg 22.1 GB/day)"
    assert ev["DETAIL"].startswith("Top destination: AZURE_EASTUS2.") and ev["METRIC_VALUE"] == 150.0
    assert ev["DEDUPE_KEY"] == f"COST_EGRESS_SPIKE|{_T19.isoformat()}"
    rows = [*[(_at(_Y19, h), "REGION_A", "AWS", 40) for h in (9, 10, 11)], (_at(_Y19, 12), "REGION_B", "AWS", 100)]
    (ev2,) = _xfer(rows).run(_arm("19"))
    assert ev2["DETAIL"].startswith("Top destination: REGION_A.")                  # 120 by region > 100 by row
    (old,) = _xfer(rows).run(_arm("19", old=True))
    assert old["DETAIL"].startswith("Top destination: REGION_B.")                 # V163: the largest row


def test_egress_counts_the_whole_previous_day_and_only_true_egress():
    early = [(_at(_Y19, 5, 30), "R", "AWS", 60), (_at(_Y19, 6, 59), "R", "AWS", 60)]   # the old blind slice
    (ev,) = _xfer(early).run(_arm("19"))
    assert ev["METRIC_VALUE"] == 120.0
    assert _xfer([(_at(_T19, 0, 30), "R", "AWS", 500)]).run(_arm("19")) == []        # today: not complete yet
    assert _xfer([(_at(_Y19, 9), None, None, 500)]).run(_arm("19")) == []            # same-region internal
    (cloud,) = _xfer([(_at(_Y19, 9), None, "GCP", 200)]).run(_arm("19"))             # cross-cloud, no region
    assert cloud["METRIC_VALUE"] == 200.0
    assert _xfer([]).run(_arm("19")) == []
    db = _xfer([(_at(_Y19, 9), "R", "AWS", 120)])
    assert len(db.run(_arm("19"))) == 1 and db.run(_arm("19")) == []              # the key is unchanged


# ============================================================================================================
# [24] COST_IDLE_OPPORTUNITY (R1-071): a NULL timer is never-suspend; no carve-out left
# ============================================================================================================
_ARM24 = _between(_D, *_SPANS["24"])


def test_idle_arm_now_matches_the_app_actionable_set_exactly():
    app = v157._idle_app_side()
    actionable = set(app.index[app["ACTIONABLE"].astype(bool)])
    arm = v157._by_wh(v157._run_arm(_ARM24, v157._idle_tables(), v157._IDLE_NOW, v157._IDLE_PRICE))
    assert set(arm) == actionable == {"WH_A", "WH_C", "WH_D", "WH_E"}
    for wh in actionable:
        assert abs(arm[wh]["METRIC_VALUE"] - app.loc[wh, "ACTIONABLE_MONTHLY_USD"]) <= 0.05, wh
    d = arm["WH_D"]
    assert d["METRIC_VALUE"] == pytest.approx(arm["WH_C"]["METRIC_VALUE"]) == pytest.approx(485.76, abs=0.01)
    assert d["TITLE"] == "WH_D idle waste ~$486/mo: AUTO_SUSPEND disabled -> 60s"
    assert "Fix: ALTER WAREHOUSE WH_D SET AUTO_SUSPEND = 60;" in d["DETAIL"]
    assert "never-suspend warehouse is not auto-booked" in d["DETAIL"] and "adopted as that booking" not in d["DETAIL"]
    assert d["DEDUPE_KEY"] == "COST_IDLE_OPPORTUNITY|WH_D|MED|2026-09-28"
    assert "WH_G" not in arm and "WH_B" not in arm
    # the gates still hold: threshold 300 drops WH_E only
    hi = v157._by_wh(v157._run_arm(_ARM24, v157._idle_tables(threshold=300.0), v157._IDLE_NOW, v157._IDLE_PRICE))
    assert set(hi) == {"WH_A", "WH_C", "WH_D"}
    # teeth: V163's [24] still skipped WH_D
    old = v157._by_wh(v157._run_arm(_between(_D163, *_SPANS["24"]), v157._idle_tables(), v157._IDLE_NOW,
                                    v157._IDLE_PRICE))
    assert set(old) == actionable - {"WH_D"}


def test_preflight_p169_6_lists_exactly_the_newly_raised_warehouse(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("V169_OUT", "PREFLIGHT_OUT", "PART_B_OUT", "REPAIR_OUT")}
    env.update(V169_OUT=str(tmp_path / "m.sql"), PREFLIGHT_OUT=str(tmp_path / "pf.sql"))
    out = subprocess.run([sys.executable, str(ROOT / "outputs" / "gen_v169.py")], env=env, cwd=tmp_path,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    pf = (tmp_path / "pf.sql").read_text(encoding="utf-8")
    grid = pf[pf.index("-- P169.6 "):pf.index("-- P169.7 ")]
    sql = grid[grid.index("        WITH cfg AS ("):grid.rindex(";")]
    con = v157._connect(v157._idle_tables(), v157._IDLE_NOW)
    cur = con.execute(v157._to_sqlite(sql))
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    assert [(r["WAREHOUSE_NAME"], bool(r["WOULD_RAISE"])) for r in rows] == [("WH_D", True)]
    assert rows[0]["MONTHLY_USD"] == pytest.approx(485.76, abs=0.01)


# ============================================================================================================
# [29] SEC_TRUST_REGRESSION (R1-233): threshold floor 1
# ============================================================================================================
_ARM29 = _insert(_D, "29")
_TODAY29 = date(2026, 9, 29)


def _trust(thr, counts: tuple[int, int, int]) -> list[dict]:
    days = [(_TODAY29 - timedelta(days=k)).isoformat() for k in (2, 1, 0)]
    db = _TrustDb(rules={"SEC_TRUST_REGRESSION": ("HIGH", thr, 1)})
    db.trust([(d, "S1", "HIGH", n) for d, n in zip(days, counts, strict=True)])
    return db.run(_ARM29)


@pytest.mark.parametrize(("thr", "counts", "n_events"), [
    (0.0, (7, 7, 7), 0),            # no rise: nothing (V163 raised two "7 -> 7" events)
    (-5.0, (7, 7, 5), 0),
    (0.0, (7, 7, 8), 1),
    (0.5, (7, 7, 8), 1),            # (0, 1] behaves like 1
    (2.0, (7, 7, 8), 0),            # the floor never lowers a higher threshold
    (None, (7, 7, 8), 1),           # NULL = 1
])
def test_trust_regression_threshold_floor(thr, counts, n_events):
    got = _trust(thr, counts)
    assert len(got) == n_events, got
    if got:
        assert " 7 -> 8 on " in got[0]["TITLE"]


def test_trust_floor_has_teeth():
    days = [(_TODAY29 - timedelta(days=k)).isoformat() for k in (2, 1, 0)]
    db = _TrustDb(rules={"SEC_TRUST_REGRESSION": ("HIGH", 0.0, 1)})
    db.trust([(d, "S1", "HIGH", 7) for d in days])
    assert len(db.run(_insert(_D163, "29"))) == 2


def test_the_translator_fails_closed():
    with pytest.raises(AssertionError):
        _to_sqlite(_arm("08").replace(":budget_usd", ":nope", 1), _B)
    with pytest.raises(AssertionError):
        _to_sqlite(_arm("12").replace("CURRENT_DATE())\n", "CURRENT_DATE()::TIMESTAMP_NTZ)\n", 1), 0)
