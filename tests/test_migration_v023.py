"""Lock for V023's PROD-only volume scope: BOTH companies' PROD databases
stay alerting; non-prod goes quiet. The predicate is scraped from the
migration and evaluated against the real database names so a future edit
can't silently drop Trexis PROD (the question this test exists to answer)."""

from __future__ import annotations

import re
from pathlib import Path

from app.companies import TREXIS_DATABASES, classify_environment

_SQL = (Path(__file__).resolve().parents[1] / "snowflake" / "migrations"
        / "V023__prod_scoped_volume.sql").read_text(encoding="utf-8")


def _predicate_matches(db: str) -> bool:
    """Evaluate the migration's PROD predicate exactly as Snowflake would."""
    exact = re.search(r"UPPER\(d\.DATABASE_NAME\) IN \(([^)]+)\)", _SQL)
    assert exact, "exact-name clause missing from V023"
    names = {n.strip().strip("'") for n in exact.group(1).split(",")}
    like = re.search(r"UPPER\(d\.DATABASE_NAME\) LIKE '%(!_PRD)' ESCAPE '!'", _SQL)
    assert like, "suffix clause missing from V023"
    return db.upper() in names or db.upper().endswith("_PRD")


def test_trexis_prod_databases_keep_alerting():
    for db in ("TRXS_EDW_PRD", "TRXS_GW_DATA_PRD", "TRXS_ABC_METADATA_PRD"):
        assert _predicate_matches(db), f"{db} must stay in the volume scan"
        assert db in TREXIS_DATABASES  # and it really is a known Trexis DB


def test_alfa_prod_databases_keep_alerting():
    for db in ("ALFA_EDW_PRD", "ALFA_EDW_MGM"):
        assert _predicate_matches(db), f"{db} must stay in the volume scan"


def test_nonprod_databases_go_quiet():
    for db in ("ALFA_EDW_DEV", "ALFA_EDW_SAN", "ALFA_EDW_SIT", "ALFA_EDW_SEA",
               "TRXS_GW_DATA_DEV", "TRXS_EDW_SIT", "TRXS_ABC_METADATA_DEV", "ADMIN"):
        assert not _predicate_matches(db), f"{db} must NOT raise volume alerts"


def test_predicate_agrees_with_app_environment_semantics():
    """The migration's SQL and the app's classify_environment must be the
    same rule — one definition of PROD, everywhere."""
    for db in ("TRXS_EDW_PRD", "ALFA_EDW_PRD", "ALFA_EDW_MGM", "TRXS_GW_DATA_PRD",
               "ALFA_EDW_DEV", "TRXS_EDW_SIT", "ADMIN"):
        assert _predicate_matches(db) == (classify_environment(db) == "PROD"), db


def test_scan_v9_has_no_phantom_credentials_columns():
    # Comments retell the discovery, so strip them: the LIVE SQL must never
    # reference the columns this account's CREDENTIALS view lacks.
    live = "\n".join(ln for ln in _SQL.splitlines() if not ln.lstrip().startswith("--"))
    assert "cr.DELETED_ON" not in live
    assert "cr.EXPIRES_AT" not in live
    assert "cr.EXPIRATION_DATE" in live
