"""v4.137 locks: every exact query row can open its Snowsight profile."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app.core import query as query_module
from app.data import insights_sql, mart_sql
from app.ui import components

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_profile_helper_supports_custom_ids_and_skips_blank_only_frames(monkeypatch):
    calls: list[str] = []

    class _ContextResult:
        df = pd.DataFrame({"ORG": ["MY_ORG"], "ACCT": ["MY_ACCOUNT"]})

        @staticmethod
        def usable() -> bool:
            return True

    def _run(sql: str, **_kwargs):
        calls.append(sql)
        return _ContextResult()

    fake_st = SimpleNamespace(
        session_state={},
        column_config=SimpleNamespace(
            LinkColumn=lambda label, **kwargs: {"label": label, **kwargs}),
    )
    monkeypatch.setattr(components, "st", fake_st)
    monkeypatch.setattr(query_module, "run", _run)

    blank = pd.DataFrame({"query_id": ["", None, pd.NA]})
    unchanged, no_config = components.snowsight_profile_column(
        blank, "Admin", id_col="query_id")
    assert unchanged is blank
    assert no_config == {}
    assert calls == []

    frame = pd.DataFrame({"query_id": ["01abc-123", None]})
    linked, config = components.snowsight_profile_column(
        frame, "Admin", id_col="query_id", label="Slowest profile")
    assert linked["PROFILE"].tolist() == [
        "https://app.snowflake.com/my_org/my_account/#/compute/history/queries/"
        "01abc-123/profile",
        None,
    ]
    assert config["PROFILE"]["label"] == "Slowest profile"
    assert len(calls) == 1


def test_new_query_profile_surfaces_are_wired_at_the_real_frame_shape():
    unit_costs = _source("app/ui/pages/cost_parts/unit_costs.py")
    assert "qdf = q_res.df.copy()" in unit_costs
    assert "qdf, _q_cfg = snowsight_profile_column(qdf, _PAGE)" in unit_costs
    assert unit_costs.index("kdf = build_call_tree(kdf, _cid)") < unit_costs.index(
        "kdf, _tree_cfg = snowsight_profile_column(kdf, _PAGE)")
    assert '"STEP", "QUERY_ID", "PROFILE", "STEP_PREVIEW"' in unit_costs

    measured_sql = insights_sql.measured_query_costs(30)
    children_sql = insights_sql.call_children_costs("root-query")
    assert "q.QUERY_ID" in measured_sql
    assert "SELECT a.QUERY_ID" in children_sql

    admin = _source("app/ui/pages/admin.py")
    assert 'snowsight_profile_column(_tel, _PAGE, id_col="query_id")' in admin
    assert admin.count('id_col="SLOWEST_QUERY_ID", label="Slowest profile"') == 3


def test_admin_performance_queries_return_exact_representative_query_ids():
    persisted = mart_sql.app_statement_stats_telemetry(7)
    fleet = mart_sql.fleet_query_stats(7)
    warehouse = mart_sql.app_statement_stats(7)
    exact = "MAX_BY(QUERY_ID, IFF(QUERY_ID IS NOT NULL, ELAPSED_MS, NULL))"
    assert exact in persisted and exact in fleet
    assert "MAX_BY(QUERY_ID, TOTAL_ELAPSED_TIME) AS SLOWEST_QUERY_ID" in warehouse
