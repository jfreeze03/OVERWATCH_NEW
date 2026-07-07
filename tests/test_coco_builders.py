"""CoCo prevention-pack diagnostic builders."""

from app.data import cost_sql, insights_sql, ops_sql, security_sql


def test_cloud_services_ratio_builder():
    sql = cost_sql.cloud_services_ratio_by_warehouse(9999, "Trexis")
    assert "WAREHOUSE_METERING_HISTORY" in sql
    assert "CLOUD_SVC_PCT" in sql and "'ELEVATED'" in sql and "'NORMAL'" in sql
    assert "DATEADD('day', -90" in sql          # clamped
    assert "WH_TRXS" in sql                     # company scope via warehouse names


def test_compile_heavy_builder():
    sql = cost_sql.compile_heavy_families(30, "ALFA")
    assert "COMPILATION_TIME" in sql and "QUERY_PARAMETERIZED_HASH" in sql
    assert "HAVING COUNT(*) >= 20" in sql


def test_poor_pruning_builder_scoped():
    sql = ops_sql.poor_pruning_queries(30, "ALFA", "ALFA_DW", "CLAIMS")
    assert "PARTITIONS_TOTAL >= 100" in sql
    assert "> 0.8" in sql
    assert "UPPER(DATABASE_NAME) IN ('ALFA_DW')" in sql
    assert "SCHEMA_NAME ILIKE '%CLAIMS%'" in sql


def test_result_cache_builder():
    sql = ops_sql.result_cache_daily(30)
    assert "BYTES_SCANNED" in sql and "HIT_PCT" in sql


def test_concurrency_peaks_builder():
    sql = ops_sql.warehouse_concurrency_peaks(30, "Trexis")
    assert "WAREHOUSE_LOAD_HISTORY" in sql
    assert "PEAK_QUEUED" in sql and "AVG_RUNNING" in sql


def test_copy_failures_builder():
    sql = ops_sql.copy_load_failures(7, "ALFA")
    assert "ACCOUNT_USAGE.COPY_HISTORY" in sql
    assert "'Load failed', 'Partially loaded'" in sql
    assert "'FAILED' AS STATUS" in sql          # colored by styled_table


def test_failed_login_reasons_builder():
    sql = security_sql.failed_login_reasons(30, "ALFA")
    assert "LOGIN_HISTORY" in sql and "IS_SUCCESS = 'NO'" in sql
    assert "'NETWORK POLICY'" in sql
    assert "KEBARR1" in sql                     # company override carried


def test_admin_role_activity_builder():
    sql = security_sql.admin_role_activity(9999)
    assert "ROLE_NAME IN ('ACCOUNTADMIN', 'SNOW_ACCOUNTADMINS')" in sql
    assert "DATEADD('day', -90" in sql


def test_storage_waste_builder():
    sql = insights_sql.storage_waste("ALFA", min_gb=2)
    assert "TABLE_STORAGE_METRICS" in sql and "TABLE_DML_HISTORY" in sql
    assert "'STALE'" in sql and "'ACTIVE'" in sql
    assert str(2 * 1024 ** 3) in sql            # min-size floor in bytes
    # company never interpolates raw — unknown values fall through to no-op scope
    assert "DROP--" not in insights_sql.storage_waste("ALFA'; DROP--")
