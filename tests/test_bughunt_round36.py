"""Bug-hunt round 36: storage optimization / retention / clone / time-travel / failsafe.

Multi-agent adversarial sweep (6 finder dimensions -> per-finder refute -> completeness critic; 2 finder
dims returned empty = floor signal, and the byte-unit basis was audited consistent binary app-wide).
3 fixes shipped; the full real-time retention-read guard, the storage-mart freshness gate, and two
unverified critic leads are deferred to a documented follow-up.

R36-CLONE-SORT [MED]  storage_reclaim (the Enterprise PRIMARY storage-waste frame) ORDER BY (TT + FS +
        RETAINED_FOR_CLONE_BYTES) DESC omitted COALESCE on the clone term. That column is NULL for
        never-cloned tables (the common case), so TT + FS + NULL = NULL, and Snowflake sorts NULLS-FIRST
        under DESC -> never-cloned tables flooded the top-50 and evicted genuine time-travel/fail-safe
        waste. The sibling table_storage_breakdown and the V124 loader both COALESCE this exact column.
R36-TTFC-01 [LOW]  the STALE-tables KPI help called a time-travel+fail-safe+clone sum "the clearest
        reduce-retention candidates" though only the time-travel share is reducible via retention. Now
        carries the same caveat the sibling KPI already has.
R36-RET-01 [MED, partial]  the retention remediation labeled a LAGGED ACCOUNT_USAGE retention value
        "verified current" and gave no lag caveat on the ALTER-generating path, so a retention lowered
        within the ~1-2h AU lag could let the panel propose RAISING it back (A3 direction) + book a
        fictitious saving. Dropped the false "verified" claim and warn about the lag before execute; the
        full fix (a real-time SHOW TABLES read at decision time, like tighten_suspend_plan) is deferred.
"""

from __future__ import annotations

from pathlib import Path

from app.data import insights_sql

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# --- R36-CLONE-SORT: NULL clone bytes no longer nullify the sort key --------
def test_round36_storage_reclaim_coalesces_clone_bytes():
    sql = insights_sql.storage_reclaim("ALL")
    # the ORDER BY clone term is COALESCEd, so a NULL clone value cannot NULL the whole key
    assert ("ORDER BY (m.TIME_TRAVEL_BYTES + m.FAILSAFE_BYTES + "
            "COALESCE(m.RETAINED_FOR_CLONE_BYTES, 0)) DESC") in sql
    # the raw (uncoalesced) sort term is gone
    assert "+ m.RETAINED_FOR_CLONE_BYTES) DESC" not in sql
    # the projected clone GB is COALESCEd too (no NaN for never-cloned tables)
    assert "ROUND(COALESCE(m.RETAINED_FOR_CLONE_BYTES, 0) / POWER(1024, 3), 2) AS CLONE_RETAINED_GB" in sql


def test_round36_storage_reclaim_matches_the_sibling_coalesce_contract():
    # the twin builder (and the V124 loader) COALESCE this exact column; storage_reclaim now agrees
    breakdown = insights_sql.table_storage_breakdown("ALL", "DB1")
    assert "COALESCE(m.RETAINED_FOR_CLONE_BYTES, 0)" in breakdown        # sibling contract
    assert "COALESCE(m.RETAINED_FOR_CLONE_BYTES, 0)" in insights_sql.storage_reclaim("ALL")


# --- R36-TTFC-01: STALE KPI help carries the time-travel-only caveat -------
def test_round36_stale_kpi_help_scopes_reducible_to_time_travel():
    spend = _read("app/ui/pages/cost_parts/spend.py")
    # the STALE KPI no longer calls the whole non-active sum "the clearest reduce-retention candidates"
    assert "the clearest\n                 \"reduce-retention candidates" not in spend
    assert "the clearest reduce-retention candidates" not in spend
    # it now states only the time-travel share is reducible (matching the sibling KPI caveat)
    assert "is reducible via DATA_RETENTION_TIME_IN_DAYS (and only its tail past the new window)" in spend
    assert "aren't deletable and shrink only once" in spend


# --- R36-RET-01: current-retention framing (superseded by the round-37 live read) ---
def test_round36_retention_panel_uses_live_setting_not_a_verified_lagged_one():
    optimize = _read("app/ui/pages/cost_parts/optimize.py")
    # the lagged storage-scan value is no longer called "verified current"
    assert "verified current" not in optimize
    # the round-37 hardening reads the current retention LIVE (INFORMATION_SCHEMA.TABLES) for the
    # direction decision, superseding the round-36 partial "lag warning" mitigation
    assert "insights_sql.table_retention_live(" in optimize
    assert "read live from" in optimize
