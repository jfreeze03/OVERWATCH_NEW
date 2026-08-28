"""Locks for bug round 3 fixes (docs/reviews/BUG_ROUND_3_2026-07-30.md).
R3-7 (pace partial-day) is covered behaviorally in test_formulas.py.
R3-4 (fail-predicate <> 'SUCCESS') is DEFERRED to V062 (app+mart parity) — not here.
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_r3_1_triage_allclear_requires_wh_daily_ok():
    cr = _read("app/ui/pages/control_room.py")
    assert "sources_ok = alerts.ok and tasks.ok and wh_daily.ok" in cr
    assert "spend anomalies not scanned" in cr           # failed read demotes the green banner


def test_r3_2_query_detail_cache_pct_is_percent():
    ins = _read("app/data/insights_sql.py")
    assert "PERCENTAGE_SCANNED_FROM_CACHE * 100 AS CACHE_PCT" in ins
    assert "PERCENTAGE_SCANNED_FROM_CACHE AS CACHE_PCT" not in ins   # the raw-fraction form is gone


def test_r3_3_snowsight_ctx_not_cached_on_failed_probe():
    cmp = _read("app/ui/components.py")
    # C35/F27 moved the probe into the shared _snowsight_ctx helper; the R3-3
    # guarantee (a failed/empty probe is NOT cached) lives there now.
    fn = cmp.split("def _snowsight_ctx", 1)[1].split("\ndef ", 1)[0]
    assert "if not org or not acct:" in fn
    i_guard = fn.index("if not org or not acct:")
    i_cache = fn.index('st.session_state["_ow_snowsight_ctx"] = ctx')
    assert i_guard < i_cache                              # the early-return guard precedes the cache write


def test_r3_5_ack_audit_scoped_to_fresh_acks():
    al = _read("app/ui/pages/alerts.py")
    fn = al.split("def _bulk_lifecycle_sql", 1)[1].split("\ndef ", 1)[0]
    assert 'STATUS = \'ACK\' AND ACK_BY =' in fn
    assert "ACK_AT >= DATEADD('minute', -2, CURRENT_TIMESTAMP())" in fn
    assert 'state_filter = "STATUS = \'ACK\'"' not in fn   # the bare over-selecting form is gone


def test_r3_6_reverse_hint_three_way_on_fix_kind():
    al = _read("app/ui/pages/alerts.py")
    assert 'AUTO_SUSPEND" if fix_kind.startswith("Tighten")' in al
    assert 'STATEMENT_TIMEOUT" if fix_kind.startswith("Statement")' in al
    # the old binary ternary that never reached AUTO_SUSPEND is gone
    assert '"STATEMENT_TIMEOUT" if "STATEMENT_TIMEOUT" in stmt_cl else "CLUSTER_RANGE"' not in al
