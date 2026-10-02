"""R2-042 + R2-103: the COST_CONTRACT_BREACH paging arm and its app twin (mart_sql.contract_exhaustion, the Overview /
Brief / Cost-verdict runway) read the SAME contract.

Both gate TOTAL on a parsable CONTRACT_START_DATE (r33 [3] in the app; the alert only since V169), bound CONSUMED to
the term [CONTRACT_START_DATE, CONTRACT_END_DATE) -- the end EXCLUSIVE, the app's contract_pace clock -- and use the
canonical trailing-30-complete-day burn. The proc is read from the LATEST migration that defines
SP_ALERT_SCAN_DAILY (last definition wins, as on the live account), never a pinned file, so a future re-derivation
cannot silently drop the start gate or the end bound (the round-13 class). Fragments compare with whitespace
collapsed: the two sides indent differently.
"""

from __future__ import annotations

from app.data import mart_sql
from tests.test_alert_rule_consistency import _latest_proc_bodies

_START_GATE = "IFF(TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_START_DATE', VALUE, NULL))) IS NULL, 0,"
_END_BOUND = ("AND DAY < COALESCE( (SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_END_DATE', VALUE, NULL))) "
              "FROM DBA_MAINT_DB.OVERWATCH.SETTINGS), '9999-12-31'::DATE)")
_TERM_END = ("(SELECT TRY_TO_DATE(MAX(IFF(KEY = 'CONTRACT_END_DATE', VALUE, NULL))) FROM DBA_MAINT_DB.OVERWATCH.SETTINGS) "
             "AS TERM_END")
_BURN = ("DAY BETWEEN DATEADD('day', -30, CURRENT_DATE())", "NULLIF(COUNT(DISTINCT DAY), 0)")


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def _arm16() -> str:
    body = _latest_proc_bodies()["SP_ALERT_SCAN_DAILY"]
    i = body.index("    -- [16] COST_CONTRACT_BREACH")
    return body[i:body.index("    -- [12] COST_STORAGE_SURGE", i)]


def _missing(app: str, alert: str) -> list[tuple[str, str]]:
    return [(side, frag) for frag in (_START_GATE, _END_BOUND, _TERM_END, *_BURN)
            for side, sql in (("app", _flat(app)), ("alert", _flat(alert))) if _flat(frag) not in sql]


def test_the_alert_and_the_app_runway_read_the_same_contract():
    assert _missing(mart_sql.contract_exhaustion(), _arm16()) == []


def test_both_sides_keep_the_settings_keys_and_neither_reads_a_literal_term():
    for side in (mart_sql.contract_exhaustion(), _arm16()):
        for key in ("'CONTRACT_CREDITS'", "'CONTRACT_START_DATE'", "'CONTRACT_END_DATE'"):
            assert key in side, key
        assert "/ 30" not in side.replace("-30", "")


def test_the_parity_lock_has_teeth():
    """Dropping the start gate from the alert, or the end bound from the app, breaks the parity."""
    app, alert = _flat(mart_sql.contract_exhaustion()), _flat(_arm16())
    assert _missing(app, alert.replace(_flat(_START_GATE), "")) == [("alert", _START_GATE)]
    assert _missing(app.replace(_flat(_END_BOUND), ""), alert) == [("app", _END_BOUND)]
    # the V163 arm (pre-V169) had neither: the lock would have caught it
    from tests._source import read
    v163 = read("snowflake/migrations/V163__ai_runaway_trust_regression.sql")
    old = v163[v163.index("    -- [16] COST_CONTRACT_BREACH"):v163.index("    -- [12] COST_STORAGE_SURGE")]
    assert {f for side, f in _missing(app, old) if side == "alert"} == {_START_GATE, _END_BOUND, _TERM_END}
