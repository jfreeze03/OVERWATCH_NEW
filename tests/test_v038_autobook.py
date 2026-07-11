"""Locks for V038 — the savings ledger books itself (owner ask 2026-07-11:
the manual ledger sat empty because changes happen in Snowsight, not the
app; detection already existed in the V024 warehouse-change scan)."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIG = (_ROOT / "snowflake" / "migrations" / "V038__ledger_autobook.sql").read_text(encoding="utf-8")


def test_v038_guard_shape_and_chain():
    assert "EXCEPTION (-20038" in _MIG and "RAISE not_ready;" in _MIG
    assert "RAISE EXCEPTION (" not in _MIG                # the V035 lesson holds
    assert "IF (v < 37) THEN" in _MIG and "SELECT 38 AS VERSION" in _MIG
    assert "ADD COLUMN IF NOT EXISTS SOURCE_CHANGE_ID" in _MIG
    assert "CALL DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK();" in _MIG  # instant first pass
    assert "AFTER DBA_MAINT_DB.OVERWATCH.TASK_WAREHOUSE_CHANGE_SCAN" in _MIG
    tail = _MIG.split("TASK_LEDGER_AUTOBOOK RESUME", 1)[1]
    assert "TASK_WAREHOUSE_CHANGE_SCAN RESUME" in tail    # root resumes after child lands


def test_autobook_books_only_cost_levers_and_dedupes():
    body = _MIG.split("SP_LEDGER_AUTOBOOK()", 1)[1]
    assert "NOT EXISTS" in body and "l.SOURCE_CHANGE_ID = r.CHANGE_ID" in body
    assert "'AUTO_SUSPEND'" in body and "'MAX_CLUSTERS'" in body
    assert "'ECONOMY'" in body and "'SIZE'" in body
    assert "999999999" in body                            # NULL values never book
    assert "'ESTIMATED'" in body                          # booked as pipeline, $0
    assert "no invented numbers" in _MIG or "0," in body  # nothing fabricated at booking


def test_autobook_settles_forward_only_with_measured_dollars():
    body = _MIG.split("UPDATE DBA_MAINT_DB.OVERWATCH.SAVINGS_LEDGER", 1)[1]
    assert "l.STATE = 'ESTIMATED'" in body                # settled items never rewrite
    assert "r.VERDICT <> 'PENDING'" in body               # only measured verdicts settle
    assert "SAVED_MONTHLY_USD >= 5" in body               # $5/mo noise floor
    assert "'AUTO:TASK_LEDGER_AUTOBOOK'" in body          # auditable settler
    assert "* :rate * 30" in body                         # measured credits/day x rate x 30
    assert "CREDIT_PRICE_USD" in _MIG                     # rate from SETTINGS, not hardcoded


def test_teardown_covers_the_new_objects():
    td = (_ROOT / "snowflake" / "teardown.sql").read_text(encoding="utf-8")
    assert "DROP TASK IF EXISTS DBA_MAINT_DB.OVERWATCH.TASK_LEDGER_AUTOBOOK;" in td
    assert "DROP PROCEDURE IF EXISTS DBA_MAINT_DB.OVERWATCH.SP_LEDGER_AUTOBOOK();" in td
    assert "TASK_LEDGER_AUTOBOOK   SUSPEND;" in td


def test_ui_and_reader_carry_the_source_and_the_reframe():
    from app.data import mart_sql
    sql = mart_sql.savings_ledger()
    assert "IFF(SOURCE_CHANGE_ID IS NULL, 'manual', 'auto') AS SOURCE" in sql
    opt = (_ROOT / "app" / "ui" / "pages" / "cost_parts" / "optimize.py").read_text(encoding="utf-8")
    assert "Books itself since V038" in opt               # the reframe caption
    assert '"SOURCE"' in opt                              # auto/manual visible per row
    assert "needs migration V038" in opt                  # honest empty state
    adm = (_ROOT / "app" / "ui" / "pages" / "admin.py").read_text(encoding="utf-8")
    assert "ledger autobook" in adm
    val = (_ROOT / "snowflake" / "validate.sql").read_text(encoding="utf-8")
    import re
    m = re.search(r"V001\.\.V(\d+) applied", val)
    assert m and int(m.group(1)) >= 38                    # floor, not a pin (tip moves)
