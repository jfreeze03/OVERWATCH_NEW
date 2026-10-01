"""House law 2 (V044), enforced on every CURRENT definer: no raw ``IFF(<db> LIKE 'TRXS%', 'Trexis', 'ALFA')``.

The guess labels every unmapped database ALFA (V044 says UNKNOWN), labels a 'TRXS' database without the underscore
Trexis, and ignores COMPANY_SCOPE overrides. V067 #22 removed it from SP_ALERT_SCAN; V172 removed the last three
current definers that still carried it (SP_CHANGE_IMPACT_SCAN arms 1a/1b, SP_ANOMALY_SWEEP's PIPE_DT_FAILURES /
PIPE_VOLUME_DROP / DQ_BREACH arms, SP_SCAN_SCHEMA_DRIFT). Company comes from COMPANY_FOR_DATABASE (or another
COMPANY_FOR_* UDF) on a plain column.

"Current definer" = the LAST ``CREATE OR REPLACE`` of each PROCEDURE / VIEW / FUNCTION across the version-ordered
migrations (how the live account resolves a re-derived object). The allowlist is EMPTY and must stay empty.
"""

from __future__ import annotations

import re
from pathlib import Path

_MIG = Path(__file__).resolve().parents[1] / "snowflake" / "migrations"
_DEF_RE = re.compile(r"CREATE\s+OR\s+REPLACE\s+(?:SECURE\s+)?(?:PROCEDURE|VIEW|FUNCTION)\s+DBA_MAINT_DB\.OVERWATCH\."
                     r"(\w+)", re.I)
_GUESS_RE = re.compile(r"LIKE\s+'TRXS%'\s*,\s*'Trexis'\s*,\s*'ALFA'", re.I)
_ALLOWLIST: frozenset[str] = frozenset()


def _version(p: Path) -> int:
    return int(re.match(r"V(\d+)", p.name).group(1))


def _definitions(files: list[Path]) -> dict[str, tuple[str, str]]:
    """{object: (file, its text up to the next definition)} for the LAST definition of each object."""
    latest: dict[str, tuple[str, str]] = {}
    for path in sorted(files, key=_version):
        text = path.read_text(encoding="utf-8")
        found = list(_DEF_RE.finditer(text))
        for k, m in enumerate(found):
            end = found[k + 1].start() if k + 1 < len(found) else len(text)
            latest[m.group(1).upper()] = (path.name, text[m.start():end])
    return latest


def _offenders(latest: dict[str, tuple[str, str]]) -> dict[str, str]:
    return {name: f for name, (f, body) in latest.items() if _GUESS_RE.search(body) and name not in _ALLOWLIST}


def test_no_current_definer_guesses_company_from_a_trxs_prefix():
    bad = _offenders(_definitions(list(_MIG.glob("V[0-9]*__*.sql"))))
    assert not bad, ("A current definer stamps COMPANY with the raw TRXS%/ALFA guess -- use "
                     f"DBA_MAINT_DB.OVERWATCH.COMPANY_FOR_DATABASE(<plain column>) (V044 / V067 #22): {bad}")


def test_the_guard_sees_the_defect_it_guards():
    """Teeth: without V172 the three V172 procs are flagged at their V140 / V150 / V133 definers."""
    files = [p for p in _MIG.glob("V[0-9]*__*.sql") if _version(p) < 172]
    assert {"SP_CHANGE_IMPACT_SCAN", "SP_ANOMALY_SWEEP", "SP_SCAN_SCHEMA_DRIFT"} <= set(_offenders(_definitions(files)))
    latest = _definitions(list(_MIG.glob("V[0-9]*__*.sql")))
    assert len(latest) >= 50                                        # the scan sees the house's objects (54 today)
    for name in ("SP_CHANGE_IMPACT_SCAN", "SP_ANOMALY_SWEEP", "SP_SCAN_SCHEMA_DRIFT"):
        assert "COMPANY_FOR_DATABASE(" in latest[name][1], name


def test_allowlist_is_empty():
    assert not _ALLOWLIST
