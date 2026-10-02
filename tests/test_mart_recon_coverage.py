"""Next-Fifty #25 (v4.589.0): the mart-vs-live reconciliation covers the facts the chargeback / company
economics / Compare surfaces are built from — warehouse credits (FACT_WAREHOUSE_DAILY) on the core recon,
and the AI facts (FACT_AI_USAGE_DAILY) in a separate, probe-gated statement — and a NULL drift (live side 0)
no longer reads OK while the mart still holds credits."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.data import mart_sql
from app.data.canary import CANARIES, EXPECTED_GAPS
from app.ui.pages.admin import _recon_state

_ROOT = Path(__file__).resolve().parents[1]
_COLS = ["CHECK_NAME", "UNIT", "FACT_VALUE", "LIVE_VALUE", "DRIFT_PCT"]


def test_recon_adds_the_warehouse_arm():
    sql = mart_sql.mart_vs_live_recon()
    for part in ("FACT_WAREHOUSE_DAILY", "SUM(CREDITS_TOTAL)", "SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY",
                 "WAREHOUSE_ID > 0", "Warehouse credits (28d"):
        assert part in sql, part
    assert sql.count("UNION ALL") == 2
    assert sql.count("WAREHOUSE_NAME IS NOT NULL") == 1          # the query-count arm's filter, unchanged


def test_ai_recon_mirrors_the_loader():
    sqlglot = pytest.importorskip("sqlglot")
    sql = mart_sql.mart_vs_live_ai_recon()
    for part in ("SOURCE IN ('Snowsight', 'CLI')", "SOURCE = 'Functions'",
                 "CORTEX_CODE_SNOWSIGHT_USAGE_HISTORY", "CORTEX_CODE_CLI_USAGE_HISTORY",
                 "CORTEX_AI_FUNCTIONS_USAGE_HISTORY", "USAGE_TIME::DATE", "START_TIME::DATE",
                 "SUM(COALESCE(TOKEN_CREDITS, 0))", "SUM(COALESCE(CREDITS, 0))",
                 "DATEADD('day', -31", "DATEADD('day', -3,"):
        assert part in sql, part
    assert "FLATTEN" not in sql                                  # the live side is the INDEPENDENT answer
    for s in (sql, mart_sql.mart_vs_live_recon()):
        assert sqlglot.parse_one(s, read="snowflake").named_selects == _COLS


def test_ai_recon_central_days_mirror_the_v167_loader():
    """R2-052 (V167): behind has_migration(167) the recon's Cortex Code day key is the latest SP_LOAD_MARTS_V27
    arm [9] key (alias aside); the Functions key (START_TIME is LTZ) and the unconverted default are unchanged."""
    sqlglot = pytest.importorskip("sqlglot")
    from tests.test_alert_rule_consistency import _latest_proc_bodies
    sql = mart_sql.mart_vs_live_ai_recon(central_days=True)
    key = "CONVERT_TIMEZONE('America/Chicago', USAGE_TIME)::DATE"
    assert sql.count(key) == 2 and "USAGE_TIME::DATE" not in sql.replace(key, "")
    assert sql.count("START_TIME::DATE") == 2                   # LTZ: the session (Central) day already
    assert "CONVERT_TIMEZONE('America/Chicago', c.USAGE_TIME)::DATE AS DAY" in _latest_proc_bodies()["SP_LOAD_MARTS_V27"]
    assert mart_sql.mart_vs_live_ai_recon(central_days=False) == mart_sql.mart_vs_live_ai_recon()
    default = mart_sql.mart_vs_live_ai_recon()
    assert default.count("USAGE_TIME::DATE") == 2 and key not in default          # the pre-V167 loader mirror
    assert sqlglot.parse_one(sql, read="snowflake").named_selects == _COLS
    # only the day keys move: the converted SQL is the default with the two keys swapped
    assert sql.replace(key, "USAGE_TIME::DATE") == mart_sql.mart_vs_live_ai_recon()


def test_admin_runs_ai_recon_behind_the_toggle():
    src = (_ROOT / "app/ui/pages/admin.py").read_text(encoding="utf-8")
    after = src.split('key="adm_recon_on"', 1)[1]
    assert "mart_sql.mart_vs_live_ai_recon(central_days=has_migration(167, _PAGE))" in after
    assert 'key="mart_recon_ai"' in after
    assert "probe=True" in after.split("mart_vs_live_ai_recon", 1)[1][:200]
    assert after.index('key="mart_recon"') < after.index("mart_vs_live_ai_recon")


@pytest.mark.parametrize(("row", "want"), [
    ({"FACT_VALUE": 101, "LIVE_VALUE": 100, "DRIFT_PCT": 1.0}, "OK"),
    ({"FACT_VALUE": 103, "LIVE_VALUE": 100, "DRIFT_PCT": 3.0}, "WARN"),
    ({"FACT_VALUE": 106, "LIVE_VALUE": 100, "DRIFT_PCT": 6.0}, "BAD"),
    ({"FACT_VALUE": 5, "LIVE_VALUE": 0, "DRIFT_PCT": None}, "BAD"),       # mart holds orphans; live is 0
    ({"FACT_VALUE": 0, "LIVE_VALUE": 0, "DRIFT_PCT": None}, "OK"),
    ({"FACT_VALUE": 0.004, "LIVE_VALUE": 0.001, "DRIFT_PCT": 300.0}, "OK"),   # sub-0.01-credit noise
    ({"FACT_VALUE": 5, "LIVE_VALUE": 0, "DRIFT_PCT": float("nan")}, "BAD"),
])
def test_recon_state_bands(row, want):
    assert _recon_state(pd.Series(row)) == want


def test_ai_recon_is_a_declared_cortex_gap():
    assert "cortex.mart_vs_live_ai_recon" in {n for n, _ in CANARIES}
    assert "cortex.mart_vs_live_ai_recon" in EXPECTED_GAPS
