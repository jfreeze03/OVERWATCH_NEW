"""rec #21: CI canary for the cost-service taxonomy.

There was no coverage test over ``cost_coverage.service_category`` — which is how
the v4.158.0 PIPE / AUTO_CLUSTERING key typos shipped (both real types silently
mapped to "Other" and were flagged as coverage gaps). This pins that every
CURRENTLY-KNOWN Snowflake metering / organization service type resolves to a
named category, so a future key typo or rename fails the gate instead of quietly
decaying completeness. It cannot see brand-new Snowflake types (no live query),
which is what ``material_unmapped_services`` covers at runtime on Admin.
"""

from __future__ import annotations

import pandas as pd

from app.logic import cost_coverage as cc

# Real service-type strings Snowflake emits into METERING_DAILY_HISTORY /
# ORGANIZATION_USAGE that the app must categorize (non-AI). If Snowflake renames
# one or the dict drifts, this list is the single place to update — and the test
# makes that a conscious edit, not a silent "Other".
KNOWN_NON_AI_SERVICE_TYPES = (
    "WAREHOUSE_METERING",
    "WAREHOUSE_METERING_READER",
    "PIPE",
    "SNOWPIPE_STREAMING",
    "SERVERLESS_TASK",
    "SERVERLESS_ALERTS",
    "AUTO_CLUSTERING",
    "MATERIALIZED_VIEW",
    "SEARCH_OPTIMIZATION",
    "QUERY_ACCELERATION",
    "SNOWPARK_CONTAINER_SERVICES",
    "COPY_FILES",
    "OPENFLOW_COMPUTE_SNOWFLAKE",
    "LOGGING",
    "TRUST_CENTER",
    "DATA_QUALITY_MONITORING",
    "REPLICATION",
    "STORAGE",
)

# AI-family service types must resolve to the single AI bucket priced at the AI
# rate — including CoCo, whose name carries no CORTEX/AI token (the v4.158.0 bug).
KNOWN_AI_SERVICE_TYPES = (
    "CORTEX_FUNCTIONS",
    "AI_SERVICES",
    "SNOWFLAKE_COCO_SNOWSIGHT",
    "SNOWFLAKE_INTELLIGENCE",
)


def test_every_known_non_ai_service_type_is_categorized():
    unmapped = [s for s in KNOWN_NON_AI_SERVICE_TYPES if cc.service_category(s) == "Other"]
    assert not unmapped, f"known service types fell to 'Other': {unmapped}"


def test_known_ai_service_types_resolve_to_ai_bucket():
    for service in KNOWN_AI_SERVICE_TYPES:
        assert cc.service_category(service) == "AI / Cortex", service


def test_case_insensitive_categorization():
    # Snowflake casing varies; the classifier upper-cases before lookup.
    assert cc.service_category("auto_clustering") == "Serverless"
    assert cc.service_category("Warehouse_Metering") == "Warehouse"


def test_material_unmapped_services_flags_other_material_rows():
    inv = pd.DataFrame(
        {
            "SERVICE_TYPE": ["WAREHOUSE_METERING", "SOME_NEW_METER"],
            "CATEGORY": ["Warehouse", "Other"],
            "MATERIALITY": ["Material", "Material"],
        }
    )
    flagged = cc.material_unmapped_services(inv)
    assert list(flagged["SERVICE_TYPE"]) == ["SOME_NEW_METER"]


def test_material_unmapped_services_ignores_below_threshold_other():
    inv = pd.DataFrame(
        {
            "SERVICE_TYPE": ["TINY_NEW_METER"],
            "CATEGORY": ["Other"],
            "MATERIALITY": ["Below threshold"],
        }
    )
    assert cc.material_unmapped_services(inv).empty


def test_material_unmapped_services_empty_frame_is_safe():
    assert cc.material_unmapped_services(pd.DataFrame()).empty
