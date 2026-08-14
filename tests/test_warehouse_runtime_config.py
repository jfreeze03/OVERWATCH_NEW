"""Warehouse-runtime deployment contract for the Streamlit app."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_project_definition_stays_warehouse_runtime_shaped() -> None:
    project = (_ROOT / "snowflake.yml").read_text(encoding="utf-8").lower()
    assert "query_warehouse: wh_alfa_admin" in project
    for container_only in ("runtime_name:", "compute_pool:", "artifact_repositories:"):
        assert container_only not in project
    assert "environment.yml" in project


def test_owner_reset_unsets_artifacts_before_switching_runtime() -> None:
    reset = (
        _ROOT / "snowflake" / "warehouse_runtime_reset.sql"
    ).read_text(encoding="utf-8")
    unset = "UNSET ARTIFACT_REPOSITORIES"
    runtime = "RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'"
    warehouse = "QUERY_WAREHOUSE = WH_ALFA_ADMIN"
    assert reset.index(unset) < reset.index(runtime)
    assert warehouse in reset
    assert "CREATE " not in reset and "DROP " not in reset
