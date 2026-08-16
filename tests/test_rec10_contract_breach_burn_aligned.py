"""rec#10: the COST_CONTRACT_BREACH paging alert's DAILY_BURN is aligned to the
canonical trailing-30-complete-day burn (SUM / NULLIF(COUNT(DISTINCT DAY)) over
today-30..today-1), NOT the old literal /30.

The fix landed in V064 and is carried by every later re-derivation of the alert
proc; this test locks it so a future SP_ALERT_SCAN re-derivation can't silently
reintroduce the /30 (which biased burn low and could suppress the breach). No
V081 was needed — the gap audit re-flagged this from a stale docstring and the
immutable /30 in the pre-V064 migration history.
"""

import re
from pathlib import Path

_MIG = Path(__file__).resolve().parents[1] / "snowflake" / "migrations"


def _version(path: Path) -> int:
    return int(re.match(r"V(\d+)", path.name).group(1))


def _latest_burn_migration() -> Path:
    # the authoritative alert burn is defined by the newest migration carrying it
    defs = [p for p in _MIG.glob("V*.sql") if "AS DAILY_BURN" in p.read_text(encoding="utf-8")]
    assert defs, "no migration defines DAILY_BURN"
    return max(defs, key=_version)


def test_latest_alert_burn_uses_count_distinct_day_not_literal_30():
    src = _latest_burn_migration().read_text(encoding="utf-8")
    assert "AS DAILY_BURN" in src
    # canonical divisor: complete days actually present, not a literal 30
    assert "/ NULLIF(COUNT(DISTINCT DAY), 0)" in src
    # and today's partial excluded: window ends today-1
    assert "DATEADD('day', -1, CURRENT_DATE())" in src
    # the old literal must be gone from the authoritative definition
    assert "COALESCE(SUM(CREDITS_BILLED), 0) / 30" not in src


def test_no_post_fix_migration_reintroduces_the_literal_30_burn():
    # V014..V062 are immutable history that legitimately carried the /30; every
    # migration from V063 on (post-V064 fix) must stay free of the /30 burn literal.
    for path in _MIG.glob("V*.sql"):
        if _version(path) < 63:
            continue
        assert "COALESCE(SUM(CREDITS_BILLED), 0) / 30" not in path.read_text(encoding="utf-8"), path.name
