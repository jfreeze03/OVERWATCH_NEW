"""V091 — auto-resolve cleared alerts. Structure, safety, and byte-derivation."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_V091 = (_ROOT / "snowflake" / "migrations" / "V091__alert_auto_clear.sql").read_text(encoding="utf-8")
_V087 = (_ROOT / "snowflake" / "migrations" / "V087__security_posture_rule.sql").read_text(encoding="utf-8")

# The three rules opted into auto-clear — ALL live-window (DEDUPE_KEY carries
# CURRENT_DATE, re-evaluated every scan), never a day-stamped historical fact.
_SEEDED = ("PERF_QUERY_FAIL_PCT", "PERF_QUEUED_MINUTES", "PERF_SPILL_GB")


def test_v091_version_guard_and_footer():
    assert "V091 requires V090 first" in _V091
    assert "IF (v < 90) THEN" in _V091
    assert "SELECT 91 AS VERSION," in _V091
    assert "WHERE VERSION = 91);" in _V091


def test_v091_adds_columns_and_seeds_only_live_window_rules():
    assert "ADD COLUMN IF NOT EXISTS AUTO_CLEAR_ENABLED BOOLEAN DEFAULT FALSE" in _V091
    assert "ADD COLUMN IF NOT EXISTS CLEAR_THRESHOLD_NUM NUMBER(18,4)" in _V091
    # seed enables exactly the three live-window rules and no others
    assert ("WHERE RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')"
            in _V091)
    # every seeded rule is CURRENT_DATE-keyed (live) in the raise arms, never day-stamped
    for rule in _SEEDED:
        assert f"c.RULE_ID = '{rule}'" in _V091           # its raise arm survives
    # a day-stamped fact rule must NOT be in the seed
    for fact_rule in ("COST_DAILY_CREDITS", "COST_WH_DAILY_CREDITS", "PIPE_COPY_FAILURES"):
        assert f"'{fact_rule}'" not in (
            "WHERE RULE_ID IN ('PERF_QUERY_FAIL_PCT', 'PERF_QUEUED_MINUTES', 'PERF_SPILL_GB')")


def test_v091_auto_clear_sweep_is_safe():
    # the sweep exists and marks a distinct machine-close kind
    assert "[auto-clear sweep] V091" in _V091
    assert "RESOLUTION_KIND = 'AUTO_CLEARED'" in _V091
    # OPEN-only: never touches ACK/RESOLVED/SNOOZED
    assert "WHERE ev.STATUS = 'OPEN'" in _V091
    # gated on the opt-in flag
    assert "WHERE ENABLED AND AUTO_CLEAR_ENABLED" in _V091
    # hysteresis floor (default 0.9 x threshold) — not the raise threshold
    assert "COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)" in _V091
    assert _V091.count("COALESCE(c.CLEAR_THRESHOLD_NUM, c.THRESHOLD_NUM * 0.9)") == 3  # one per seeded rule
    # anti-flap dwell + today's-bucket only (never rewrites a prior-day fact)
    assert "ev.RAISED_AT <= DATEADD('hour', -1, CURRENT_TIMESTAMP())" in _V091
    assert "ev.DEDUPE_KEY LIKE '%|' || CURRENT_DATE()" in _V091
    # a sweep failure must not break the scan (own try/except, does not touch :fails)
    _sweep = _V091[_V091.find("RESOLUTION_KIND = 'AUTO_CLEARED'"):][:3000]
    assert "autoclear_sweep_failed" in _sweep
    assert "fails := fails + 1" not in _sweep   # a sweep failure is not a rule-block failure


def test_v091_is_byte_derived_from_v087():
    # the SP body between V087 and V091 differs ONLY by the added sweep: every V087
    # raise arm + the SUPERSEDED escalation sweep survive verbatim (CR-normalized).
    v087 = _V087.replace("\r\n", "\n")
    v091 = _V091.replace("\r\n", "\n")
    superseded_sweep = ("UPDATE DBA_MAINT_DB.OVERWATCH.ALERT_EVENTS lo\n"
                        "           SET STATUS = 'RESOLVED', RESOLVED_AT = CURRENT_TIMESTAMP(), "
                        "RESOLUTION_KIND = 'SUPERSEDED'")
    assert superseded_sweep in v087 and superseded_sweep in v091
    # all five hardcoded raise arms carried over unchanged
    for arm in ("[01] COST_DAILY_CREDITS", "[03] PERF_QUERY_FAIL_PCT",
                "[05] PERF_SPILL_GB", "[11] COST_CLOUD_SVC_RATIO", "[21]"):
        assert arm in v091


def test_read_path_excludes_auto_cleared_like_superseded():
    mart = (_ROOT / "app" / "data" / "mart_sql.py").read_text(encoding="utf-8")
    # every machine-close exclusion now covers BOTH kinds (precision, MTTR, resolutions, fatigue)
    assert mart.count("NOT IN ('SUPERSEDED', 'AUTO_CLEARED')") == 4
    assert "COALESCE(RESOLUTION_KIND, '') <> 'SUPERSEDED'" not in mart  # no bare-superseded left
