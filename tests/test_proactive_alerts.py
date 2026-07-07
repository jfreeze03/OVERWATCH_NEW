"""V011 prevention pack: five new rules, nothing regressed from v3."""

from pathlib import Path

_V011 = (Path(__file__).resolve().parents[1] / "snowflake" / "migrations"
         / "V011__proactive_alerts.sql").read_text(encoding="utf-8")

_NEW_RULES = ("COST_CLOUD_SVC_RATIO", "COST_STORAGE_SURGE", "COST_SERVERLESS_CREEP",
              "PIPE_COPY_FAILURES", "SEC_BREAK_GLASS_USE")


def test_v011_seeds_all_five_rules():
    for rule in _NEW_RULES:
        assert f"'{rule}'" in _V011, rule
    assert "SELECT 11 AS VERSION" in _V011


def test_v011_scan_v4_carries_every_v3_block():
    """CREATE OR REPLACE of the scan must never drop earlier rules."""
    for marker in ("SEC_CRED_EXPIRY",          # V009 credentials block
                   "FACT_METERING_DAILY",      # V007 budget blocks
                   "alert scan v4 complete"):
        assert marker in _V011, marker


def test_v011_sources_and_dedupe():
    assert "WAREHOUSE_METERING_HISTORY" in _V011
    assert "DATABASE_STORAGE_USAGE_HISTORY" in _V011
    assert "METERING_DAILY_HISTORY" in _V011
    assert "ACCOUNT_USAGE.COPY_HISTORY" in _V011
    assert "ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')" in _V011
    # serverless creep excludes what other rules already watch
    assert "'WAREHOUSE_METERING', 'WAREHOUSE_METERING_READER', 'AI_SERVICES'" in _V011
    # weekly-recurring dedupe for creep; daily for the rest
    assert "DATE_TRUNC('week', CURRENT_DATE())" in _V011
