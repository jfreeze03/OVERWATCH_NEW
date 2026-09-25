"""Next-Fifty #5 (v4.589.0): one warehouse change must never be booked twice. The app used to INSERT a
manual ESTIMATED ledger row after a guarded resize / auto-suspend change, and the daily change scan's
SP_LEDGER_AUTOBOOK (V038/V145) booked the SAME change again and settled it on measured actuals — the
manual twin stayed ESTIMATED forever (inflating the pipeline) or got verified a second time. Now a
manual row the settled auto row supersedes is excluded from every read + rollup and offered for an
audited REJECT cleanup, and the app stops booking the autobooked levers itself."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

import app.logic.actions as actions_mod
from app import config
from app.config import LEDGER_TWIN_MATCH_DAYS
from app.data import mart_sql
from app.logic.actions import ledger_totals, savings_by_lever, savings_by_month, split_superseded

_ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_twin_rule_shape():
    sqlglot = pytest.importorskip("sqlglot")
    sql = mart_sql.savings_ledger(limit=None)
    assert "AS SUPERSEDED_BY_CHANGE_ID" in sql
    assert "m.SOURCE_CHANGE_ID IS NULL" in sql and "a.SOURCE_CHANGE_ID = r.CHANGE_ID" in sql
    assert "a.STATE <> 'ESTIMATED'" in sql                       # only a SETTLED auto row supersedes
    assert "IFF(UPPER(TRIM(m.FINDING_TYPE)) = 'RESIZE', 'SIZE'" in sql
    assert f"DATEADD('day', {LEDGER_TWIN_MATCH_DAYS}, m.CREATED_AT)" in sql
    assert "IN ('AUTO_SUSPEND', 'MAX_CLUSTERS', 'RESIZE')" in sql
    assert "'SCHEDULE'" not in sql                               # the scan cannot see a schedule
    assert "LIMIT" not in sql                                    # the economics read stays uncapped
    sqlglot.parse_one(sql, read="snowflake")


def test_summary_excludes_twins_from_every_aggregate():
    sql = mart_sql.savings_summary_quarter()
    assert sql.count("t.TWIN_ITEM_ID IS NULL") == 5
    for alias in ("VERIFIED_QTD_USD", "VERIFIED_ITEMS", "VERIFIED_ACTIVE_MONTHLY_USD",
                  "VERIFIED_ACTIVE_ITEMS", "ESTIMATED_OPEN_USD"):
        head = sql.split(f"AS {alias}", 1)[0]
        assert "t.TWIN_ITEM_ID IS NULL" in head.rsplit(" AS ", 1)[-1], alias
    assert "AS SUPERSEDED_ITEMS" in sql
    assert "LEFT JOIN twin t ON t.TWIN_ITEM_ID = l.ITEM_ID" in sql


def test_ledger_totals_excludes_superseded():
    rows = pd.DataFrame([
        {"STATE": "ESTIMATED", "ESTIMATED_USD": 120, "VERIFIED_USD": None, "SUPERSEDED_BY_CHANGE_ID": "CHG1"},
        {"STATE": "VERIFIED", "ESTIMATED_USD": 0, "VERIFIED_USD": 100, "SUPERSEDED_BY_CHANGE_ID": None},
        {"STATE": "VERIFIED", "ESTIMATED_USD": 90, "VERIFIED_USD": 80, "SUPERSEDED_BY_CHANGE_ID": "CHG2"},
        {"STATE": "ESTIMATED", "ESTIMATED_USD": 50, "VERIFIED_USD": None, "SUPERSEDED_BY_CHANGE_ID": None},
        {"STATE": "REJECTED", "ESTIMATED_USD": 30, "VERIFIED_USD": None, "SUPERSEDED_BY_CHANGE_ID": "CHG3"},
    ])
    t = ledger_totals(rows)
    assert (t["estimated_usd"], t["verified_usd"]) == (50.0, 100.0)
    assert (t["estimated_count"], t["verified_count"]) == (1, 1)
    assert t["superseded_count"] == 2                 # the REJECTED twin is already cleaned up
    assert t["superseded_estimated_usd"] == 210.0
    assert t["realization_pct"] is None               # the only live verified row carried no estimate
    assert ledger_totals(pd.DataFrame())["superseded_count"] == 0


def test_split_superseded_tolerates_missing_column_and_blanks():
    no_col = pd.DataFrame([{"STATE": "VERIFIED"}])
    live, sup = split_superseded(no_col)
    assert len(live) == 1 and sup.empty
    blanks = pd.DataFrame({"STATE": ["A", "B", "C", "D"], "SUPERSEDED_BY_CHANGE_ID": [None, "", "  ", "CHG"]})
    live, sup = split_superseded(blanks)
    assert list(live["STATE"]) == ["A", "B", "C"] and list(sup["STATE"]) == ["D"]
    live, sup = split_superseded(None)
    assert live.empty and sup.empty


def test_month_and_lever_rollups_drop_superseded(monkeypatch):
    monkeypatch.setattr(actions_mod, "account_now", lambda: datetime(2026, 9, 24, 9, 0))
    rows = pd.DataFrame([
        {"STATE": "VERIFIED", "VERIFIED_USD": 100, "ESTIMATED_USD": 100, "VERIFIED_AT": "2026-07-15",
         "FINDING_TYPE": "RESIZE", "SUPERSEDED_BY_CHANGE_ID": None},
        {"STATE": "VERIFIED", "VERIFIED_USD": 900, "ESTIMATED_USD": 900, "VERIFIED_AT": "2026-07-20",
         "FINDING_TYPE": "RESIZE", "SUPERSEDED_BY_CHANGE_ID": "CHG9"},
    ])
    by_month = savings_by_month(rows)
    assert float(by_month["VERIFIED_USD"].sum()) == 100.0
    by_lever = savings_by_lever(rows)
    assert float(by_lever["VERIFIED_USD"].sum()) == 100.0


def test_verification_runs_hide_autobook_and_twin_proposals():
    sqlglot = pytest.importorskip("sqlglot")
    sql = mart_sql.savings_verification_runs()
    assert "L.SOURCE_CHANGE_ID IS NULL" in sql and "t.TWIN_ITEM_ID IS NULL" in sql
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY V.ITEM_ID ORDER BY V.RUN_AT DESC) = 1" in sql
    sqlglot.parse_one(sql, read="snowflake")


def test_verified_wins_excludes_twins():
    for company in ("ALL", "Trexis"):
        assert "t.TWIN_ITEM_ID IS NULL" in mart_sql.verified_wins(company)
    assert "'Trexis'" in mart_sql.verified_wins("Trexis")        # the company scope is still applied


def test_supersede_sql_is_idempotent_and_audited():
    sqlglot = pytest.importorskip("sqlglot")
    s = mart_sql.supersede_ledger_twins_sql("'JDOE'")
    assert "SET STATE = 'REJECTED'" in s and "AND l.STATE <> 'REJECTED'" in s
    assert "superseded by auto-measured change" in s and "'JDOE'" in s
    assert "VERIFIED_USD" not in s.split("SET", 1)[1].split("FROM", 1)[0]   # audit trail kept
    sqlglot.parse_one(s, read="snowflake")


def test_optimize_stops_double_booking_and_offers_cleanup():
    src = _src("app/ui/pages/cost_parts/optimize.py")
    assert "from app.config import LEDGER_AUTOBOOKED_LEVERS" in src
    assert "_autobooked = (_lever in LEDGER_AUTOBOOKED_LEVERS and remediation.autobook_books_change(" in src
    assert src.count('write_gate_open("ledger_twin_reject")') == 1
    assert src.count('stamp_write("ledger_twin_reject", ok)') == 1
    assert "mart_sql.supersede_ledger_twins_sql(identity_sql())" in src
    assert 'get("SUPERSEDED_ITEMS")' in src                      # the UNCAPPED count, not len() of the frame
    assert "split_superseded(res.df)" in src
    assert config.LEDGER_AUTOBOOKED_LEVERS == ("AUTO_SUSPEND", "MAX_CLUSTERS", "RESIZE")


def test_roi_section_discloses_superseded():
    ds = _src("app/ui/decision_studio.py")
    assert "totals['superseded_count']" in ds and "superseded by the " in ds


# --- Next-Fifty #11 (V153): the autobook settles on the FULL 14-day window; the app shows the re-measure ---
_V153_SQL = "snowflake/migrations/V153__ledger_autobook_full_window_settle.sql"
_RN_RE = re.compile(r"ROW_NUMBER\(\) OVER \(\s*PARTITION BY r\.WAREHOUSE_NAME.*?ORDER BY r\.CHANGE_SEEN_AT, "
                    r"r\.CHANGE_ID\)", re.S)


def test_savings_ledger_exposes_full_window_remeasure():
    sqlglot = pytest.importorskip("sqlglot")
    from app.data.common import account_today_sql
    sql = mart_sql.savings_ledger(limit=None)
    for alias in ("AS MEASURED_AFTER_DAYS", "AS REMEASURED_14D_MONTHLY_USD", "AS VOLUME_RATIO",
                  "AS VOLUME_CONFOUNDED"):
        assert alias in sql, alias
    # the closed-window gate mirrors V153 D4 on the app clock (the TZ standard), never session CURRENT_DATE()
    assert f"AND {account_today_sql()} > r.TRACKING_UNTIL" in sql
    assert "r.VERDICT IN ('IMPROVED', 'NEUTRAL', 'REGRESSED', 'NO_BASELINE', 'INSUFFICIENT_AFTER')" in sql
    assert "AND r.AFTER_CREDITS_PER_DAY IS NOT NULL" in sql and "CURRENT_DATE()" not in sql
    # the LBA-1 RN is the proc's window, compared whitespace-normalized (the layouts differ)
    proc = _src(_V153_SQL)
    proc_rn = {" ".join(m.split()) for m in _RN_RE.findall(proc)}
    app_rn = {" ".join(m.split()) for m in _RN_RE.findall(sql)}
    assert len(app_rn) == 1 and app_rn <= proc_rn, (app_rn, proc_rn)
    # FLOAT rate from SETTINGS (the proc's exact read) and the $5 floor before the RN=1 / 0 split
    assert "TRY_TO_DOUBLE(MAX(IFF(KEY = 'CREDIT_PRICE_USD'" in sql and "TRY_TO_NUMBER" not in sql
    remeasure = sql.split("AS REMEASURED_14D_MONTHLY_USD", 1)[0].rsplit("AS MEASURED_AFTER_DAYS", 1)[1]
    assert "* px.RATE * 30 >= 5," in remeasure and "ROUND((COALESCE(r.BASELINE_CREDITS_PER_DAY, 0)" in remeasure
    assert "NOT BETWEEN 0.7 AND 1.3" in sql
    assert "LIMIT" not in sql and "ORDER BY l.CREATED_AT DESC" in sql      # uncapped; RN over the whole ledger
    assert "IFF(l.SOURCE_CHANGE_ID IS NULL, 'manual', 'auto') AS SOURCE" in sql
    sqlglot.parse_one(sql, read="snowflake")
    sqlglot.parse_one(mart_sql.savings_ledger(), read="snowflake")


def test_ledger_totals_discloses_volume_confounded_and_auto_pending():
    rows = pd.DataFrame([
        {"STATE": "VERIFIED", "ESTIMATED_USD": 0, "VERIFIED_USD": 100, "VOLUME_CONFOUNDED": True, "SOURCE": "auto"},
        {"STATE": "VERIFIED", "ESTIMATED_USD": 0, "VERIFIED_USD": 50, "VOLUME_CONFOUNDED": False, "SOURCE": "auto"},
        {"STATE": "VERIFIED", "ESTIMATED_USD": 0, "VERIFIED_USD": 25, "VOLUME_CONFOUNDED": None, "SOURCE": "manual"},
        {"STATE": "VERIFIED", "ESTIMATED_USD": 0, "VERIFIED_USD": 10, "VOLUME_CONFOUNDED": "TRUE", "SOURCE": "auto"},
        # a superseded twin never counts, even when flagged
        {"STATE": "VERIFIED", "ESTIMATED_USD": 0, "VERIFIED_USD": 999, "VOLUME_CONFOUNDED": True,
         "SOURCE": "manual", "SUPERSEDED_BY_CHANGE_ID": "CHG1"},
        {"STATE": "ESTIMATED", "ESTIMATED_USD": 0, "VERIFIED_USD": None, "VOLUME_CONFOUNDED": None, "SOURCE": "auto"},
        {"STATE": "ESTIMATED", "ESTIMATED_USD": 40, "VERIFIED_USD": None, "VOLUME_CONFOUNDED": None,
         "SOURCE": "manual"},
    ])
    t = ledger_totals(rows)
    assert (t["volume_confounded_count"], t["volume_confounded_usd"]) == (2, 110.0)
    assert t["verified_usd"] == 185.0                   # disclosure only: nothing is subtracted
    assert (t["auto_settle_pending_count"], t["estimated_count"]) == (1, 2)
    empty = ledger_totals(pd.DataFrame())
    assert (empty["volume_confounded_count"], empty["volume_confounded_usd"],
            empty["auto_settle_pending_count"]) == (0, 0.0, 0)
    # an older read without the V153 columns degrades to zero, never raises
    legacy = ledger_totals(pd.DataFrame([{"STATE": "VERIFIED", "VERIFIED_USD": 5, "ESTIMATED_USD": 0}]))
    assert (legacy["volume_confounded_count"], legacy["auto_settle_pending_count"]) == (0, 0)


def test_alert_closed_loop_notes_are_lever_aware_and_adoptable():
    src = _src("app/ui/pages/alerts.py")
    assert "separate change-scan row" not in src and "verifier measures actuals" not in src
    assert "adopts" in src and "when the scan sees a saving-direction change" in src
    # ledger_for_event matches NOTES LIKE '%event <id8>%': the substring must survive
    assert "_cl_note = ('From alert event ' + event_id[:8]" in src
    assert "f\"{sql_literal(_cl_note)}, \"" in src
    assert "; verify with a proof run on the Savings ledger." in src          # STATEMENT_TIMEOUT
    assert "; the daily change scan adopts and settles it on " in src          # AUTO_SUSPEND / MAX_CLUSTERS


def test_optimize_shows_the_remeasure_and_keeps_auto_rows_out_of_hand_verify():
    src = _src("app/ui/pages/cost_parts/optimize.py")
    assert '"REMEASURED_14D_MONTHLY_USD"' in src and '"MEASURED_AFTER_DAYS"' in src
    # no explicit format on the $ column: the _USD suffix keeps format_usd + the em-dash for NULL
    assert '"REMEASURED_14D_MONTHLY_USD": st.column_config' not in src
    assert "_ledger_view[\"VOLUME_CONFOUNDED\"].map(_yes_no_dash)" in src
    assert "15–16 days after the change" in src and "3.68 → 4" in src
    verify = src.split('with st.expander("Verify an estimated item (proof required)"):', 1)[1][:900]
    assert "split_superseded(res.df)[0]" in verify
    assert '_live = _live[_live["SOURCE"].astype(str) != "auto"]' in verify


def test_roi_splits_auto_settling_items_from_awaiting_proof():
    ds = _src("app/ui/decision_studio.py")
    assert 'totals.get("auto_settle_pending_count")' in ds
    assert "settle automatically when their " in ds and "14-day window closes" in ds
    assert "totals.get(\"volume_confounded_count\")" in ds
    assert "whose 14-day measured window shows query " in ds and "Counted as measured, not adjusted." in ds
